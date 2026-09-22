"""DS-7 误差分析：研究性高水位事件、峰值低估与滞后、确定性风险规则。

流程（顺序固定）：

1. 读入 DS-2b 的小时水位表与 DS-4b 的特征网格 / 预测序列 / 实验指标（只读）；
2. 只用 **train 段**拟合研究性高水位阈值 ``research_high_water``（默认 p90）；
3. 在 validation / test 段的每一网格上，把**全部 7 类模型**（持久性基线 + E1 / E2 的
   Ridge、XGBoost、LSTM）放在同一张表、同一套事件 / 峰值 / 提前量口径下对比；
4. 逐样本计算事件级精确率 / 召回率 / F1、峰值低估量、最大绝对误差、峰值滞后与提前量；
5. 对 ``2022-09-30T17:00~23:00`` 涨落过程逐点核对，并分别统计涨水段与退水段的平均误差；
6. 用 p80（6.99 m）做**敏感性附录**；
7. 写出严格 JSON 证据与确定性人读报告（无时间戳、无绝对路径、同一输入两次运行一致）。

口径与限制：

- 术语统一为 **research_high_water（研究性高水位）**：阈值由 train 段分位数拟合，
  **不是官方警戒 / 超警标准**，不得据此下确定性结论；
- 预测时域为 1 / 3 / 6 **步**（1 小时网格），提前量单位为**小时**，上限 6 小时；
- 观测事件为 0 的格子三项事件指标一律 ``null`` + ``reason="no_observed_events"``，不填 0；
- 脚本只读输入，不回写任何上游产物。

用法::

    python scripts/run_hourly_error_analysis.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from river_sentinel_ml.dataset import chronological_split
from river_sentinel_ml.events import (
    RESEARCH_HIGH_WATER,
    RESEARCH_HIGH_WATER_NOTE,
    EventSegment,
    earliest_warning_lead,
    evaluate_event_metrics,
    event_segments,
    fit_research_high_water_threshold,
    label_research_high_water,
    peak_lag_hours,
    peak_underestimation,
)
from river_sentinel_ml.metrics import evaluate_forecasts

REPO_ROOT = Path(__file__).resolve().parents[1]
PROCESSED = REPO_ROOT / "data" / "processed"

DEFAULT_HOURLY_CSV = PROCESSED / "water_level_hourly_cata12720.csv"
DEFAULT_BASELINE_JSON = PROCESSED / "hourly_baseline_metrics.json"
DEFAULT_FEATURE_CSV = PROCESSED / "hourly_features_cata12720.csv"
DEFAULT_EXPERIMENTS_JSON = PROCESSED / "hourly_model_experiments.json"
DEFAULT_PREDICTIONS_DIR = PROCESSED / "hourly_model_predictions"
DEFAULT_OUTPUT_JSON = PROCESSED / "hourly_error_analysis.json"
DEFAULT_REPORT_MD = REPO_ROOT / "docs" / "experiments" / "hourly-error-and-events.md"

SCHEMA_VERSION = "ds7-hourly-error-analysis-1"
HORIZONS: tuple[int, ...] = (1, 3, 6)
SEGMENTS: tuple[str, ...] = ("train", "validation", "test")
EXPERIMENTS: tuple[str, ...] = ("E1", "E2")
MODEL_STEMS: tuple[str, ...] = ("ridge", "xgboost", "lstm")
PERSISTENCE = "persistence"

#: DS-6 方案 B 后三类学习模型的样本集已对齐，主口径直接覆盖全部 7 类模型。
LEARNED_MODELS: tuple[str, ...] = tuple(
    f"{stem}_{experiment}" for experiment in EXPERIMENTS for stem in MODEL_STEMS
)
ALL_MODELS: tuple[str, ...] = (PERSISTENCE, *LEARNED_MODELS)

#: 预测序列必须齐全：2 个实验 x 3 个模型 x 3 个 horizon x 3 个段 = 54 个 CSV。
EXPECTED_PREDICTION_FILES: int = len(EXPERIMENTS) * len(MODEL_STEMS) * len(HORIZONS) * len(SEGMENTS)

#: 与 DS-6 产物交叉校验的允许绝对误差（指标单位 m；实测最大绝对差量级 1e-7）。
CROSS_CHECK_TOLERANCE = 1e-5

#: 缺失处理路径：只有 Ridge / LSTM 做训练段中位数填充；XGBoost 用树原生缺失处理。
NATIVE_MISSING_STEMS: tuple[str, ...] = ("xgboost",)
MISSING_HANDLING: dict[str, str] = {
    "ridge": "train-median imputation",
    "xgboost": "native tree handling (missing=NaN)",
    "lstm": "train-median imputation",
}

#: 报告展示用的中文标签；JSON 仍保留 MISSING_HANDLING 的英文原值，便于与 DS-6 产物逐字核对。
MISSING_HANDLING_LABELS: dict[str, str] = {
    "ridge": "训练段中位数填充",
    "xgboost": "树原生缺失处理",
    "lstm": "训练段中位数填充",
}

#: §7 表格的逐列说明（由 render_report 在两个实验表后各插入一次）。
_COLUMN_NOTE = (
    "> 表内两对列的区别：`含缺失特征的行数` / `缺失特征单元格数` 是**原始特征矩阵上实测的缺失**；"
    "`填充行数` / `填充单元格数` 是**模型实际执行的填充**。Ridge / LSTM 用训练段中位数填充，"
    "因此这两对数值相等；XGBoost 由树原生处理缺失，填充两列**恒为 0**，其缺失只体现在前两列，"
    "不得读作「XGBoost 做了填充」。`填充/输入` = 填充单元格数 ÷ 输入单元格数"
    "（输入单元格数 = 样本数 × 该实验冻结特征列数），**行比例不能当作特征值比例使用**。"
)

THRESHOLD_QUANTILE = 0.90
SENSITIVITY_QUANTILE = 0.80
HOURLY_REQUIRED_COLUMNS: tuple[str, ...] = ("observed_at", "water_level_m", "is_imputed")
GRID_REQUIRED_COLUMNS: tuple[str, ...] = (
    "observed_at",
    "segment",
    "water_level_t",
    "is_imputed_t",
)
TRAIN_RATIO = 0.70
VALIDATION_RATIO = 0.15
MISSING_TOKEN = "null"
NO_VALID_PAIRS_REASON = "no_valid_pairs"
FOCUS_WINDOW: tuple[str, str] = ("2022-09-30T17:00:00+08:00", "2022-09-30T23:00:00+08:00")
RISING_KEY = "rising"
FALLING_KEY = "falling"

MODEL_LABELS: dict[str, str] = {
    PERSISTENCE: "持久性基线",
    "ridge_E1": "Ridge E1",
    "xgboost_E1": "XGBoost E1",
    "lstm_E1": "LSTM E1",
    "ridge_E2": "Ridge E2",
    "xgboost_E2": "XGBoost E2",
    "lstm_E2": "LSTM E2",
}

STATIC_DECLARATIONS: dict[str, str] = {
    "step_unit": (
        "步长口径（HS-D-1）：1 / 3 / 6 是 1 小时网格上的**步数**，等价于未来 1 / 3 / 6 小时；"
        "列名沿用 prediction_h{h}，提前量单位为**小时**，受最长时域约束上限为 6 小时。"
    ),
    "threshold_source": (
        "research_high_water（研究性高水位）阈值**只由 train 段**有效观测拟合"
        "（water_level_t 有限且 is_imputed_t=False），validation / test 段不参与拟合也不参与重算；"
        "**不是官方警戒 / 超警标准**，页面、报告与智能体回答都不得低于该口径。"
    ),
    "event_definition": (
        "事件定义：目标时刻实测水位 ≥ 阈值记为观测事件，模型在该时刻的预测量 ≥ 阈值记为预测事件；"
        "逐（段, horizon, 模型）按行对齐后按点统计，不做跨 horizon 合并。"
    ),
    "event_segment_convention": (
        "事件段口径：先剔除缺失与被插补的观测，在**有效观测的压缩序列**上把连续 True 合并为一段；"
        "因此跨缺失缝隙的两个合格点会被算作同一段，报告同时列出每段起止时刻以便核对。"
    ),
    "imputation_policy": (
        "缺失特征填充（DS-6 方案 B）：Ridge / LSTM 不接受缺失输入，按**各自实验冻结的特征列**"
        "（列名与列数一律以 DS-6 产物的 feature_columns 为准，E1 与 E2 分别统计、不得合并）"
        "用 **train 段对应列的中位数**填充；"
        "填充统计量只由 train 段拟合，validation / test 段不参与。"
        "XGBoost 缺失由树原生处理，**不做任何填充**（imputed_rows = imputed_cells = 0），"
        "其缺失行只以 rows_with_missing_features / missing_feature_cells 描述，**不得记为填充**。"
        "Ridge / LSTM 的 imputed_rows / imputed_cells 优先取自 DS-6 实验产物，"
        "并与本脚本在原始特征矩阵上按同一批冻结特征列的复算逐格核对（不一致即失败）。"
        "两条路径都不剔除任何 usable_h{h} 行，因此持久性基线之外的三类学习模型在每一 horizon 的"
        "样本集完全一致，同一 horizon 的目标数可比，所有 7 类模型都进入主口径。"
    ),
    "support_difference": (
        "支持集差异：持久性基线的预测行只需要滞后观测，学习模型的预测行还依赖特征可用性；"
        "逐 horizon 逐模型先在该模型自己的支持行上评分，再在**全部 7 类模型的共同支持集**上重算一次。"
    ),
    "null_metrics": (
        "空值口径：观测事件点为 0 的格子，事件级 precision / recall / f1 一律写 null 并给出 "
        "reason=no_observed_events，**不填 0**；有效配对样本为 0 的格子四项回归指标亦为 null。"
    ),
    "lead_definition": (
        "提前量口径：只看目标时刻落在该事件段窗口内、且预测量 ≥ 阈值的行，取其中**发布时间最早**的"
        "那一行，提前量 = 事件开始时刻 − 发布时间（小时）。发布时间不早于事件开始时刻时写 null"
        "（reason=crossing_after_event_start），不用 0 代替。"
    ),
    "focus_window": (
        f"重点过程：目标时刻窗口 {FOCUS_WINDOW[0]} ~ {FOCUS_WINDOW[1]}，逐点给出"
        "锚点、实测、预测、绝对误差与方向。"
        "**§5 逐点表、§5.1 涨退水统计与 JSON focus_process 共用同一个窗口**："
        "涨退水样本只取目标时刻落在该窗口内的行，窗口外的数据不参与相位统计。"
    ),
    "sensitivity": (
        "p80 敏感性附录：把阈值换成 train 段 p80（6.99 m）后重算的事件数与事件级指标，"
        "**只作敏感性分析**，不改主口径（主口径一律是 train 段 p90），不得用于结论。"
    ),
    "validation_event_caveat": (
        "validation 事件口径（用户裁决 2026-09-22 §1.3）：① validation 段曾用于早停与超参数选择，"
        "其事件级 precision / recall / F1 **仅作机制验证**，不得当作泛化性能结论；"
        "② validation 段在 train p90 阈值下只有 **3 个事件段（< 5）**，事件样本量过小，"
        "**只作描述性分析，不作结论**。真正的泛化结论必须由 test 段承担，而 test 段在本阈值下零观测事件。"
    ),
    "segment_error": (
        "逐段误差口径：事件段由网格上的连续标示点合并而成，取**目标时刻落在该段起止窗口内**的预测行，"
        "逐（模型, horizon）计算段内 MAE 与段内峰值低估量（观测峰值时刻的 实测 − 预测，正值=模型偏低）；"
        "窗口内没有成对有效行时样本数为 0，MAE 与低估量写 `null`（reason=no_valid_pairs），不用 0 代替。"
    ),
    "conservatism": (
        "结论保守性（HS-D-12）：全部数字都是**探索性 / 教学科研**结果，不得作为正式洪水预警，"
        "不得就高水位事件下确定性结论，不得给出「超警」判断。"
    ),
    "usage": (
        "使用限制：数据为历史回放 / 离线数据，不代表实时水情；本报告仅用于教学、科研与辅助分析，"
        "教学科研辅助，不替代官方防汛决策。"
    ),
}


@dataclass(frozen=True)
class SeriesKey:
    """Identity of one forecast series."""

    model: str
    horizon: int
    segment: str


# --------------------------------------------------------------------------- #
# 输入
# --------------------------------------------------------------------------- #


def load_hourly_levels(path: Path) -> pd.DataFrame:
    """Read the DS-2b hourly table with timezone-aware timestamps."""
    frame = pd.read_csv(path)
    missing = [column for column in HOURLY_REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"{path.name} is missing the columns {', '.join(missing)}")
    stamps = pd.to_datetime(frame["observed_at"], format="ISO8601")
    if not isinstance(stamps.dtype, pd.DatetimeTZDtype):
        raise ValueError("observed_at must carry a timezone offset")
    frame = frame.assign(observed_at=stamps)
    return frame.sort_values("observed_at").reset_index(drop=True)


def load_feature_grid(path: Path) -> pd.DataFrame:
    """Read the DS-4b hourly feature grid (only the columns this script needs)."""
    frame = pd.read_csv(path)
    missing = [column for column in GRID_REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"{path.name} is missing the columns {', '.join(missing)}")
    stamps = pd.to_datetime(frame["observed_at"], format="ISO8601")
    if not isinstance(stamps.dtype, pd.DatetimeTZDtype):
        raise ValueError("observed_at must carry a timezone offset")
    frame = frame.assign(observed_at=stamps, is_imputed_t=frame["is_imputed_t"].astype(bool))
    known = set(frame["segment"].astype(str))
    if not set(SEGMENTS).issubset(known):
        raise ValueError(f"{path.name} does not carry all segments {SEGMENTS}")
    return frame.sort_values("observed_at").reset_index(drop=True)


def build_persistence_frames(hourly: pd.DataFrame) -> dict[SeriesKey, pd.DataFrame]:
    """Rebuild the DS-5b persistence baseline as target-keyed rows.

    口径与 ``scripts/run_hourly_baseline.py`` 一致：先在**完整序列**上生成
    ``prediction_h{h}(t) = water_level_m(t-h)``，再按 0.70 / 0.15 / 0.15 切段，
    剔除**目标时刻**被插补的行（C-5），最后只保留实测与预测都非缺失的成对样本。
    """
    levels = pd.Series(
        hourly["water_level_m"].to_numpy(dtype="float64"),
        index=pd.DatetimeIndex(hourly["observed_at"]),
        name="water_level_m",
    )
    forecast = pd.DataFrame({"observed": levels})
    for horizon in HORIZONS:
        forecast[f"prediction_h{horizon}"] = levels.shift(horizon)
    forecast["is_imputed"] = hourly["is_imputed"].to_numpy(dtype=bool)
    forecast["observed_at"] = hourly["observed_at"].to_numpy()
    forecast["target"] = hourly["observed_at"].to_numpy()

    split = chronological_split(
        forecast, train_ratio=TRAIN_RATIO, validation_ratio=VALIDATION_RATIO
    )
    frames: dict[SeriesKey, pd.DataFrame] = {}
    for segment in SEGMENTS:
        part = getattr(split, segment)
        part = part.loc[~part["is_imputed"].to_numpy(dtype=bool)]
        for horizon in HORIZONS:
            observed = part["observed"].to_numpy(dtype="float64")
            prediction = part[f"prediction_h{horizon}"].to_numpy(dtype="float64")
            targets = pd.DatetimeIndex(part["target"])
            valid = np.isfinite(observed) & np.isfinite(prediction)
            frames[SeriesKey(PERSISTENCE, horizon, segment)] = pd.DataFrame(
                {
                    "target": targets[valid],
                    "anchor": targets[valid] - pd.Timedelta(hours=horizon),
                    "observed": observed[valid],
                    "prediction": prediction[valid],
                }
            ).reset_index(drop=True)
    return frames


def expected_prediction_paths(directory: Path) -> list[tuple[SeriesKey, Path]]:
    """Every prediction CSV DS-7 requires: 2 x 3 x 3 x 3 = 54 个序列。"""
    expected: list[tuple[SeriesKey, Path]] = []
    for experiment in EXPERIMENTS:
        for stem in MODEL_STEMS:
            for horizon in HORIZONS:
                for segment in SEGMENTS:
                    expected.append(
                        (
                            SeriesKey(f"{stem}_{experiment}", horizon, segment),
                            directory / experiment / f"{stem}_h{horizon}_{segment}.csv",
                        )
                    )
    return expected


def load_model_frames(directory: Path) -> dict[SeriesKey, pd.DataFrame]:
    """Load all 54 first-seed prediction CSVs as target-keyed rows.

    DS-6 的导出约定：``observed_at`` 是**锚点**，``observed`` 是锚点后第 h 步的实测值，
    因此目标时刻 = 锚点 + h 小时（已用 DS-4b 网格逐点核对，差值为 0）。

    输入完整性 **fail-fast**：任一预测序列缺失都抛 ``ValueError`` 并逐个列出缺失文件，
    不允许在支持集不齐的情况下继续生成「可信」结论。
    """
    expected = expected_prediction_paths(directory)
    missing = [
        f"{key.model}/h{key.horizon}/{key.segment} -> {source.relative_to(directory).as_posix()}"
        for key, source in expected
        if not source.exists()
    ]
    if missing:
        raise ValueError(
            f"预测序列缺失 {len(missing)} / {len(expected)} 个 CSV，无法继续分析："
            + "；".join(missing)
        )
    frames: dict[SeriesKey, pd.DataFrame] = {}
    for key, source in expected:
        part = pd.read_csv(source)
        anchors = pd.DatetimeIndex(pd.to_datetime(part["observed_at"], format="ISO8601"))
        frames[key] = pd.DataFrame(
            {
                "target": anchors + pd.Timedelta(hours=key.horizon),
                "anchor": anchors,
                "observed": part["observed"].to_numpy(dtype="float64"),
                "prediction": part["prediction"].to_numpy(dtype="float64"),
            }
        ).reset_index(drop=True)
    return frames


# --------------------------------------------------------------------------- #
# 网格级事件描述与逐格评分
# --------------------------------------------------------------------------- #


def event_pieces(grid: pd.DataFrame, segment: str, threshold: float) -> list[EventSegment]:
    """Locate research high-water runs of one segment on the raw hourly grid."""
    part = grid[grid["segment"].astype(str) == segment].sort_values("observed_at")
    levels = part["water_level_t"].to_numpy(dtype="float64")
    imputed = part["is_imputed_t"].to_numpy(dtype=bool)
    usable = np.isfinite(levels) & ~imputed
    stamp = pd.DatetimeIndex(part["observed_at"])[usable]
    mask = label_research_high_water(levels[usable], threshold)
    return event_segments(mask, stamp)


def event_segment_errors(
    frames: Mapping[SeriesKey, pd.DataFrame],
    grid: pd.DataFrame,
    segment: str,
    threshold: float,
    models: Sequence[str],
) -> list[dict[str, Any]]:
    """Per-event-segment MAE and peak underestimation for every model / horizon."""
    rows: list[dict[str, Any]] = []
    for piece in event_pieces(grid, segment, threshold):
        per_model: dict[str, Any] = {}
        for model in models:
            horizons: dict[str, Any] = {}
            for horizon in HORIZONS:
                frame = frames.get(SeriesKey(model, horizon, segment))
                if frame is None:
                    continue
                targets = pd.DatetimeIndex(frame["target"])
                part = frame.loc[
                    (targets >= piece.start_time) & (targets <= piece.end_time)
                ].sort_values("target")
                cell = regression_cell(part, horizon)
                peak = peak_underestimation(part["observed"], part["prediction"], part["target"])
                horizons[str(horizon)] = {
                    "sample_count": cell["sample_count"],
                    "mae": cell["mae"],
                    "observed_peak": _finite(peak.observed_peak),
                    "peak_prediction": _finite(peak.prediction_at_peak),
                    "peak_underestimation": _finite(peak.underestimation),
                    "reason": cell["reason"],
                }
            if horizons:
                per_model[model] = horizons
        rows.append(
            {
                "event_start": _moment(piece.start_time),
                "event_end": _moment(piece.end_time),
                "event_points": int(piece.length),
                "per_model": per_model,
            }
        )
    return rows


def grid_event_summary(
    grid: pd.DataFrame,
    segment: str,
    threshold: float,
) -> dict[str, Any]:
    """Count research high-water points / segments on the raw hourly grid."""
    pieces = event_pieces(grid, segment, threshold)
    part = grid[grid["segment"].astype(str) == segment].sort_values("observed_at")
    levels = part["water_level_t"].to_numpy(dtype="float64")
    imputed = part["is_imputed_t"].to_numpy(dtype=bool)
    usable = np.isfinite(levels) & ~imputed
    return {
        "segment": segment,
        "threshold": threshold,
        "hours": int(len(part)),
        "usable_observations": int(np.count_nonzero(usable)),
        "max_level": _finite(float(np.max(levels[usable])) if usable.any() else float("nan")),
        "event_points": int(sum(piece.length for piece in pieces)),
        "event_segments": len(pieces),
        "event_windows": [_window_text(item) for item in pieces],
    }


def regression_cell(frame: pd.DataFrame, horizon: int) -> dict[str, Any]:
    """MAE / RMSE / R² / NSE on the pairwise-valid rows of one series."""
    observed = frame["observed"].to_numpy(dtype="float64")
    prediction = frame["prediction"].to_numpy(dtype="float64")
    keep = np.isfinite(observed) & np.isfinite(prediction)
    if not bool(keep.any()):
        return {
            "sample_count": 0,
            "mae": None,
            "rmse": None,
            "r2": None,
            "nse": None,
            "peak_absolute_error": None,
            "reason": NO_VALID_PAIRS_REASON,
        }
    pair = pd.DataFrame({"observed": observed[keep], f"prediction_h{horizon}": prediction[keep]})
    metrics = evaluate_forecasts(pair, (horizon,))[horizon]
    return {
        "sample_count": int(metrics.sample_count),
        "mae": _finite(metrics.mae),
        "rmse": _finite(metrics.rmse),
        "r2": _finite(metrics.r2),
        "nse": _finite(metrics.nse),
        "peak_absolute_error": _finite(metrics.peak_absolute_error),
        "reason": None,
    }


def event_cell(frame: pd.DataFrame, threshold: float) -> dict[str, Any]:
    """Event-level metrics for one (segment, horizon, model) grid cell."""
    observed = frame["observed"].to_numpy(dtype="float64")
    prediction = frame["prediction"].to_numpy(dtype="float64")
    metrics = evaluate_event_metrics(
        label_research_high_water(observed, threshold),
        label_research_high_water(prediction, threshold),
    )
    return {
        "precision": metrics.precision,
        "recall": metrics.recall,
        "f1": metrics.f1,
        "tp": metrics.tp,
        "fp": metrics.fp,
        "fn": metrics.fn,
        "observed_event_points": metrics.observed_event_points,
        "observed_event_segments": metrics.observed_event_segments,
        "predicted_event_points": metrics.predicted_event_points,
        "predicted_event_segments": metrics.predicted_event_segments,
        "reason": metrics.reason,
    }


def peak_cell(frame: pd.DataFrame) -> dict[str, Any]:
    """Peak underestimation, largest absolute error and signed peak lag."""
    observed = frame["observed"].to_numpy(dtype="float64")
    prediction = frame["prediction"].to_numpy(dtype="float64")
    targets = pd.DatetimeIndex(frame["target"])
    peak = peak_underestimation(observed, prediction, targets)
    return {
        "observed_peak": peak.observed_peak,
        "observed_peak_time": _moment(peak.observed_peak_time),
        "prediction_at_peak": peak.prediction_at_peak,
        "underestimation": peak.underestimation,
        "max_absolute_error": peak.max_absolute_error,
        "max_absolute_error_time": _moment(peak.max_absolute_error_time),
        "max_absolute_error_direction": peak.max_absolute_error_direction,
        "peak_lag_hours": peak_lag_hours(observed, prediction, targets),
        "sample_count": int(np.count_nonzero(np.isfinite(observed) & np.isfinite(prediction))),
    }


def lead_table(frame: pd.DataFrame, threshold: float) -> list[dict[str, Any]]:
    """Advance time of every observed event segment that the model could foresee."""
    observed = frame["observed"].to_numpy(dtype="float64")
    prediction = frame["prediction"].to_numpy(dtype="float64")
    targets = pd.DatetimeIndex(frame["target"])
    anchors = pd.DatetimeIndex(frame["anchor"])
    rows: list[dict[str, Any]] = []
    for event in event_segments(label_research_high_water(observed, threshold), targets):
        lead = earliest_warning_lead(
            event,
            issue_times=anchors,
            target_times=targets,
            predictions=prediction,
            threshold=threshold,
        )
        rows.append(
            {
                "event_start": _moment(event.start_time),
                "event_end": _moment(event.end_time),
                "event_points": int(event.length),
                "lead_hours": lead.lead_hours,
                "first_issue_time": _moment(lead.issue_time),
                "first_target_time": _moment(lead.target_time),
                "first_prediction": _finite(lead.predicted_value),
                "reason": lead.reason,
            }
        )
    return rows


def process_rows(
    frames: Mapping[SeriesKey, pd.DataFrame],
    *,
    models: Sequence[str],
    window: tuple[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Row-level diagnostics inside the focus window, per model and horizon."""
    start = pd.Timestamp(window[0])
    end = pd.Timestamp(window[1])
    rows: dict[str, list[dict[str, Any]]] = {}
    for model in models:
        for horizon in HORIZONS:
            frame = frames.get(SeriesKey(model, horizon, "test"))
            if frame is None:
                continue
            targets = pd.DatetimeIndex(frame["target"])
            part = frame.loc[(targets >= start) & (targets <= end)].sort_values("target")
            entries: list[dict[str, Any]] = []
            for row in part.itertuples(index=False):
                observed = float(row.observed)
                prediction = float(row.prediction)
                if not (np.isfinite(observed) and np.isfinite(prediction)):
                    continue
                residual = prediction - observed
                entries.append(
                    {
                        "target": _moment(row.target),
                        "anchor": _moment(row.anchor),
                        "observed": observed,
                        "prediction": prediction,
                        "absolute_error": abs(residual),
                        "signed_error": residual,
                        "direction": _direction(residual),
                    }
                )
            if entries:
                rows[f"{model}_h{horizon}"] = entries
    return rows


def phase_summary(
    frames: Mapping[SeriesKey, pd.DataFrame],
    window: tuple[str, str],
) -> dict[str, Any]:
    """Mean error on rising versus falling samples **inside the focus window only**.

    §5 逐点表、§5.1 涨退水统计与 JSON ``focus_process`` 共用同一个目标时刻窗口：
    窗口外的行一律不进入相位统计，避免用窗口外的样本解释重点过程。
    """
    start = pd.Timestamp(window[0])
    end = pd.Timestamp(window[1])
    summary: dict[str, Any] = {
        "window": {"start": window[0], "end": window[1], "segment": "test"},
        "models": {},
    }
    for model in ALL_MODELS:
        summary["models"][model] = {}
        for horizon in HORIZONS:
            frame = frames.get(SeriesKey(model, horizon, "test"))
            if frame is None:
                summary["models"][model][str(horizon)] = {"reason": NO_VALID_PAIRS_REASON}
                continue
            targets = pd.DatetimeIndex(frame["target"])
            part = frame.loc[(targets >= start) & (targets <= end)].sort_values("target")
            summary["models"][model][str(horizon)] = _phase_cell(part)
    return summary


def _phase_cell(frame: pd.DataFrame) -> dict[str, Any]:
    """Phase statistics of rows that are already restricted to the focus window."""
    observed = frame["observed"].to_numpy(dtype="float64")
    prediction = frame["prediction"].to_numpy(dtype="float64")
    valid = np.isfinite(observed) & np.isfinite(prediction)
    if int(np.count_nonzero(valid)) < 2:
        return {
            RISING_KEY: _empty_phase(),
            FALLING_KEY: _empty_phase(),
            "reason": NO_VALID_PAIRS_REASON,
        }
    signed = prediction - observed
    difference = np.diff(observed)
    rising = np.zeros(len(observed), dtype=bool)
    falling = np.zeros(len(observed), dtype=bool)
    rising[1:] = difference > 0.0
    falling[1:] = difference < 0.0
    payload: dict[str, Any] = {"reason": None}
    for key, mask in ((RISING_KEY, rising), (FALLING_KEY, falling)):
        keep = mask & valid
        count = int(np.count_nonzero(keep))
        if count == 0:
            payload[key] = _empty_phase()
            continue
        payload[key] = {
            "sample_count": count,
            "mae": _finite(float(np.mean(np.abs(signed[keep])))),
            "mean_signed_error": _finite(float(np.mean(signed[keep]))),
        }
    return payload


def _empty_phase() -> dict[str, Any]:
    return {"sample_count": 0, "mae": None, "mean_signed_error": None}


# --------------------------------------------------------------------------- #
# 端到端证据
# --------------------------------------------------------------------------- #


def run_analysis(
    *,
    hourly_source: Path,
    grid_source: Path,
    predictions_dir: Path,
    experiments_source: Path,
    baseline_source: Path,
) -> dict[str, Any]:
    """Run the whole DS-7 chain and return a strict-JSON-serialisable payload."""
    hourly = load_hourly_levels(hourly_source)
    grid = load_feature_grid(grid_source)
    frames = build_persistence_frames(hourly)
    frames.update(load_model_frames(predictions_dir))
    experiments = json.loads(experiments_source.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_source.read_text(encoding="utf-8"))

    threshold = fit_research_high_water_threshold(grid, quantile=THRESHOLD_QUANTILE)
    sensitivity = fit_research_high_water_threshold(grid, quantile=SENSITIVITY_QUANTILE)

    regression: dict[str, Any] = {}
    events: dict[str, Any] = {}
    peaks: dict[str, Any] = {}
    leads: dict[str, Any] = {}
    common: dict[str, Any] = {}
    for segment in SEGMENTS:
        regression[segment] = {}
        events[segment] = {}
        peaks[segment] = {}
        leads[segment] = {}
        common[segment] = {}
        for horizon in HORIZONS:
            head = str(horizon)
            regression[segment][head] = {}
            events[segment][head] = {}
            peaks[segment][head] = {}
            leads[segment][head] = {}
            _require_complete_support(frames, segment, horizon)
            for model in ALL_MODELS:
                frame = frames[SeriesKey(model, horizon, segment)]
                regression[segment][head][model] = regression_cell(frame, horizon)
                events[segment][head][model] = event_cell(frame, threshold)
                peaks[segment][head][model] = peak_cell(frame)
                if segment == "validation":
                    leads[segment][head][model] = lead_table(frame, threshold)
            shared = _common_targets(
                [frames[SeriesKey(model, horizon, segment)] for model in ALL_MODELS]
            )
            common[segment][head] = _common_cell(frames, segment, horizon, shared)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "horizons": [int(horizon) for horizon in HORIZONS],
        "segments": list(SEGMENTS),
        "models": list(ALL_MODELS),
        "research_high_water": {
            "metric": RESEARCH_HIGH_WATER,
            "threshold": threshold,
            "quantile": THRESHOLD_QUANTILE,
            "fit_basis": "train 段 water_level_t 有限且 is_imputed_t=False 的观测",
            "fit_rows": _train_rows(grid),
            "unit": "m",
            "note": RESEARCH_HIGH_WATER_NOTE,
        },
        "inputs": {
            "hourly_levels": {
                "name": hourly_source.name,
                "sha256": sha256_file(hourly_source),
                "rows": int(len(hourly)),
            },
            "feature_grid": {
                "name": grid_source.name,
                "sha256": sha256_file(grid_source),
                "rows": int(len(grid)),
                "usable_per_horizon": _usable_columns(grid),
            },
            "model_experiments": {
                "name": experiments_source.name,
                "experiments": sorted(experiments.get("experiments", {})),
            },
            "baseline_metrics": {
                "name": baseline_source.name,
                "schema_version": baseline.get("schema_version"),
            },
            "predictions": {
                "name": predictions_dir.name,
                "files": EXPECTED_PREDICTION_FILES,
            },
        },
        "grid_event_summary": {
            segment: grid_event_summary(grid, segment, threshold) for segment in SEGMENTS
        },
        "regression": regression,
        "cross_check": _cross_check(frames, experiments, baseline),
        "common_support": common,
        "imputation": imputation_profile(frames, grid, experiments),
        "events": events,
        "event_segment_errors": event_segment_errors(
            frames, grid, "validation", threshold, ALL_MODELS
        ),
        "peaks": peaks,
        "leads": leads,
        "focus_process": {
            "window": {"start": FOCUS_WINDOW[0], "end": FOCUS_WINDOW[1], "segment": "test"},
            "rows": process_rows(frames, models=ALL_MODELS, window=FOCUS_WINDOW),
            "phases": phase_summary(frames, FOCUS_WINDOW),
            "max_change_in_window": _max_change(frames, FOCUS_WINDOW),
        },
        "sensitivity_p80": {
            "threshold": sensitivity,
            "quantile": SENSITIVITY_QUANTILE,
            "basis": "train 段 p80；只作敏感性附录，不改主口径",
            "grid_event_summary": {
                segment: grid_event_summary(grid, segment, sensitivity) for segment in SEGMENTS
            },
            "events": {
                segment: {
                    str(horizon): {
                        model: event_cell(frames[SeriesKey(model, horizon, segment)], sensitivity)
                        for model in ALL_MODELS
                        if SeriesKey(model, horizon, segment) in frames
                    }
                    for horizon in HORIZONS
                }
                for segment in ("validation", "test")
            },
        },
    }
    payload["declarations"] = {
        **STATIC_DECLARATIONS,
        "data_limitations": _data_limitations(payload),
        "imputation_note": _imputation_note(payload),
    }
    return payload


def _require_complete_support(
    frames: Mapping[SeriesKey, pd.DataFrame],
    segment: str,
    horizon: int,
) -> None:
    """Fail-fast unless all 7 model series of one (segment, horizon) cell exist."""
    absent = [model for model in ALL_MODELS if SeriesKey(model, horizon, segment) not in frames]
    if absent:
        raise ValueError(
            f"共同支持集无法计算：{segment} h{horizon} 缺少模型序列 {absent}，"
            f"需要全部 {len(ALL_MODELS)} 类模型"
        )


def _common_targets(frames: Sequence[pd.DataFrame]) -> list[pd.Timestamp]:
    if not frames:
        return []
    shared = set(pd.DatetimeIndex(frames[0]["target"]))
    for frame in frames[1:]:
        shared &= set(pd.DatetimeIndex(frame["target"]))
    return sorted(shared)


def _common_cell(
    frames: Mapping[SeriesKey, pd.DataFrame],
    segment: str,
    horizon: int,
    targets: Sequence[pd.Timestamp],
) -> dict[str, Any]:
    """Re-score every model on one identical target set."""
    keep = pd.DatetimeIndex(list(targets))
    if keep.empty:
        return {"sample_count": 0, "models": {}, "reason": NO_VALID_PAIRS_REASON}
    cells: dict[str, Any] = {}
    for model in ALL_MODELS:
        key = SeriesKey(model, horizon, segment)
        if key not in frames:
            raise ValueError(f"共同支持集无法计算：{segment} h{horizon} 缺少 {model} 序列")
        frame = frames[key]
        part = frame.loc[pd.DatetimeIndex(frame["target"]).isin(keep)]
        cells[model] = regression_cell(part, horizon)
    return {"sample_count": int(len(keep)), "models": cells, "reason": None}


def _max_change(
    frames: Mapping[SeriesKey, pd.DataFrame],
    window: tuple[str, str],
) -> dict[str, Any]:
    """Largest one-hour level change inside the focus window (from the observations)."""
    frame = frames.get(SeriesKey(PERSISTENCE, HORIZONS[0], "test"))
    if frame is None:
        return {"value": None, "at_target": None, "direction": None}
    targets = pd.DatetimeIndex(frame["target"])
    part = frame.loc[
        (targets >= pd.Timestamp(window[0])) & (targets <= pd.Timestamp(window[1]))
    ].sort_values("target")
    levels = part["observed"].to_numpy(dtype="float64")
    if len(levels) < 2:
        return {"value": None, "at_target": None, "direction": None}
    difference = np.diff(levels)
    position = int(np.argmax(np.abs(difference)))
    return {
        "value": _finite(float(difference[position])),
        "at_target": _moment(pd.DatetimeIndex(part["target"])[position + 1]),
        "direction": RISING_KEY if difference[position] > 0 else FALLING_KEY,
    }


def _train_rows(grid: pd.DataFrame) -> int:
    part = grid[grid["segment"].astype(str) == "train"]
    levels = part["water_level_t"].to_numpy(dtype="float64")
    imputed = part["is_imputed_t"].to_numpy(dtype=bool)
    return int(np.count_nonzero(np.isfinite(levels) & ~imputed))


def _usable_columns(grid: pd.DataFrame) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for horizon in HORIZONS:
        column = f"usable_h{horizon}"
        if column not in grid.columns:
            continue
        usable = grid[grid[column]]
        summary[f"h{horizon}"] = {
            segment: int(np.count_nonzero(usable["segment"].astype(str) == segment))
            for segment in SEGMENTS
        }
    return summary


def _data_limitations(payload: Mapping[str, Any]) -> str:
    """Measured per-segment missingness plus the inherited upstream caveats."""
    parts: list[str] = []
    for segment in SEGMENTS:
        cell = payload["grid_event_summary"][segment]
        hours = int(cell["hours"])
        missing = hours - int(cell["usable_observations"])
        share = (missing / hours * 100.0) if hours else float("nan")
        parts.append(f"{segment} 段 {hours} 小时中缺测或被插补剔除 {missing} 小时（{share:.2f}%）")
    return (
        "已知局限：数据完整性 " + "；".join(parts) + "。"
        "HS-D-5 有界前瞻按 1 小时构建，相对真实预报作业偏乐观；"
        "降雨采用 Best Match 代理变量，不等于上游站点真值；"
        "HS-D-15 的修订门槛本轮未重算，任何结论修订都需回到该门槛复核。"
    )


def _experiment_feature_columns(experiments: Mapping[str, Any]) -> dict[str, list[str]]:
    """Frozen feature columns of each DS-6 experiment; fail-fast when absent."""
    block = experiments.get("experiments", {})
    columns: dict[str, list[str]] = {}
    for experiment in EXPERIMENTS:
        declared = block.get(experiment, {}).get("feature_columns")
        if not declared:
            raise ValueError(
                f"DS-6 产物缺少 experiments.{experiment}.feature_columns，无法核对填充统计"
            )
        columns[experiment] = [str(column) for column in declared]
    return columns


def _reported_imputation(
    experiments: Mapping[str, Any],
    experiment: str,
    stem: str,
    horizon: int,
    segment: str,
) -> dict[str, int]:
    """Read DS-6's own usable_rows / missing / imputed counters for one cell."""
    cell = (
        experiments.get("experiments", {})
        .get(experiment, {})
        .get("models", {})
        .get(stem, {})
        .get(f"h{horizon}", {})
        .get(segment, {})
    )
    if not isinstance(cell, Mapping):
        raise ValueError(
            f"DS-6 产物缺少 {experiment}/{stem}/h{horizon}/{segment} 的填充统计，无法核对"
        )
    keys = ("usable_rows", "rows_with_missing_features", "imputed_rows", "imputed_cells")
    absent = [key for key in keys if key not in cell]
    if absent:
        raise ValueError(f"DS-6 产物 {experiment}/{stem}/h{horizon}/{segment} 缺少字段 {absent}")
    return {key: int(cell[key]) for key in keys}


def _share(part: int, total: int) -> float | None:
    return _finite(part / total) if total else None


def imputation_profile(
    frames: Mapping[SeriesKey, pd.DataFrame],
    grid: pd.DataFrame,
    experiments: Mapping[str, Any],
) -> dict[str, Any]:
    """Per-experiment / per-model missingness and imputation accounting (fail-fast).

    口径：

    - 特征列一律取该实验在 DS-6 产物中冻结的 ``feature_columns``（E1 / E2 分别统计，
      不得合并成一条「一致」记录）；
    - ``rows_with_missing_features`` / ``missing_feature_cells`` 是原始特征矩阵上的实测缺失；
    - Ridge / LSTM 的 ``imputed_rows`` / ``imputed_cells`` **取自 DS-6 产物**，
      并与实测缺失逐格核对，不一致直接失败；
    - XGBoost 原生处理缺失，``imputed_rows`` / ``imputed_cells`` 恒为 0，
      缺失只以 ``missing_*`` 描述，**不得写成 XGBoost 做了填充**。
    """
    experiment_columns = _experiment_feature_columns(experiments)
    index = grid.set_index("observed_at")
    profile: dict[str, Any] = {
        "policy": STATIC_DECLARATIONS["imputation_policy"],
        "experiments": {},
        "cross_check": {},
    }
    cross_rows: list[dict[str, Any]] = []
    for experiment in EXPERIMENTS:
        feature_columns = experiment_columns[experiment]
        unknown = [column for column in feature_columns if column not in grid.columns]
        if unknown:
            raise ValueError(f"{experiment} 的冻结特征列不在特征矩阵中：{unknown}")
        cells: dict[str, Any] = {}
        for segment in SEGMENTS:
            part = index[index["segment"].astype(str) == segment]
            cells[segment] = {}
            for horizon in HORIZONS:
                per_model: dict[str, Any] = {}
                for stem in MODEL_STEMS:
                    model = f"{stem}_{experiment}"
                    key = SeriesKey(model, horizon, segment)
                    if key not in frames:
                        raise ValueError(
                            f"缺少 {model} h{horizon} {segment} 的预测序列，无法统计填充规模"
                        )
                    anchors = pd.DatetimeIndex(frames[key]["anchor"])
                    rows = part.loc[anchors]
                    if len(rows) != len(anchors):
                        raise ValueError(
                            f"{model} h{horizon} {segment} 的锚点不在特征矩阵中，无法统计填充规模"
                        )
                    missing = rows[feature_columns].isna()
                    missing_rows = int(missing.any(axis=1).sum())
                    missing_cells = int(missing.sum().sum())
                    reported = _reported_imputation(experiments, experiment, stem, horizon, segment)
                    if reported["usable_rows"] != len(rows):
                        raise ValueError(
                            f"{model} h{horizon} {segment} 样本数与 DS-6 产物不一致："
                            f"预测序列 {len(rows)}，DS-6 usable_rows {reported['usable_rows']}"
                        )
                    if reported["rows_with_missing_features"] != missing_rows:
                        raise ValueError(
                            f"{model} h{horizon} {segment} 缺失行数与 DS-6 产物不一致："
                            f"复算 {missing_rows}，产物 {reported['rows_with_missing_features']}"
                        )
                    native = stem in NATIVE_MISSING_STEMS
                    if native:
                        if reported["imputed_rows"] or reported["imputed_cells"]:
                            raise ValueError(
                                f"{model} h{horizon} {segment} 使用原生缺失处理，"
                                "DS-6 产物却报告了填充计数"
                            )
                        imputed_rows, imputed_cells = 0, 0
                    else:
                        imputed_rows = reported["imputed_rows"]
                        imputed_cells = reported["imputed_cells"]
                        if (imputed_rows, imputed_cells) != (missing_rows, missing_cells):
                            raise ValueError(
                                f"{model} h{horizon} {segment} 填充统计与原始特征矩阵不一致："
                                f"复算 rows={missing_rows} cells={missing_cells}，"
                                f"DS-6 产物 rows={imputed_rows} cells={imputed_cells}"
                            )
                    input_cells = len(rows) * len(feature_columns)
                    per_model[model] = {
                        "stem": stem,
                        "experiment": experiment,
                        "feature_count": len(feature_columns),
                        "missing_handling": MISSING_HANDLING[stem],
                        "missing_handling_label": MISSING_HANDLING_LABELS[stem],
                        "native_missing_handling": native,
                        "usable_rows": len(rows),
                        "rows_with_missing_features": missing_rows,
                        "missing_feature_cells": missing_cells,
                        "missing_cell_share": _share(missing_cells, input_cells),
                        "input_cells": input_cells,
                        "imputed_rows": imputed_rows,
                        "imputed_cells": imputed_cells,
                        "imputed_cell_share": _share(imputed_cells, input_cells),
                    }
                    cross_rows.append(
                        {
                            "scope": f"{experiment}/{stem}/h{horizon}/{segment}",
                            "usable_rows": len(rows),
                            "missing_feature_cells": missing_cells,
                            "imputed_cells": imputed_cells,
                            "max_abs_difference": abs(
                                reported["imputed_cells"] - (0 if native else missing_cells)
                            ),
                        }
                    )
                cells[segment][f"h{horizon}"] = per_model
        profile["experiments"][experiment] = {
            "feature_columns": feature_columns,
            "feature_count": len(feature_columns),
            "models": [f"{stem}_{experiment}" for stem in MODEL_STEMS],
            "cells": cells,
        }
    profile["cross_check"] = {
        "scope": (
            "本脚本按 DS-6 冻结特征列在原始特征矩阵上复算缺失与填充规模，"
            "再与 DS-6 产物中 Ridge / LSTM 的 imputed_rows / imputed_cells 逐格对比；"
            "XGBoost 只核对原生缺失处理（imputed_rows = imputed_cells = 0）"
        ),
        "cells": len(cross_rows),
        "max_abs_difference": max(
            (float(row["max_abs_difference"]) for row in cross_rows), default=0.0
        ),
        "rows": cross_rows,
    }
    return profile


def _imputation_note(payload: Mapping[str, Any]) -> str:
    """Measured imputation size per experiment / model and what it therefore limits."""
    profile = payload["imputation"]
    totals: list[str] = []
    for experiment in EXPERIMENTS:
        block = profile["experiments"][experiment]
        for stem in MODEL_STEMS:
            label = MODEL_LABELS[f"{stem}_{experiment}"]
            imputed = 0
            input_cells = 0
            for segment in SEGMENTS:
                for horizon in HORIZONS:
                    cell = block["cells"][segment][f"h{horizon}"][f"{stem}_{experiment}"]
                    imputed += int(cell["imputed_cells"])
                    input_cells += int(cell["input_cells"])
            if stem in NATIVE_MISSING_STEMS:
                totals.append(f"{label} 的 imputed_rows / imputed_cells 全程恒为 0（原生缺失处理）")
                continue
            share = imputed / input_cells * 100.0
            totals.append(
                f"{label} 全段合计填充 {imputed} / {input_cells} 个输入单元格（{share:.2f}%）"
            )
    rows_text: list[str] = []
    for experiment in EXPERIMENTS:
        block = profile["experiments"][experiment]
        for horizon in HORIZONS:
            cell = block["cells"]["test"][f"h{horizon}"][f"ridge_{experiment}"]
            rows_text.append(
                f"{experiment} h{horizon} {cell['rows_with_missing_features']} / "
                f"{cell['usable_rows']} 行"
            )
    return (
        "实测规模（按各实验冻结特征列在原始特征矩阵上复算，并与 DS-6 产物逐格核对）："
        + "；".join(totals)
        + "。**多数样本行至少包含一项训练段统计填充值**（test 段 "
        + "、".join(rows_text)
        + "），但按**单元格**计，填充占全部输入单元格的比例远低于行占比（见下表「填充/输入」列），"
        "行比例不能当作特征值比例使用。XGBoost 不做填充，其 rows_with_missing_features "
        "只是树模型原生处理缺失的行数。缺失几乎全部来自水位滞后列（雨量列仅个别行缺失）。"
        "该口径保证了三类学习模型可在**同一批目标**上比较，但**限制工程外推**："
        "真实在线场景若缺测程度或缺失机制与历史不同，本轮指标不能代表那时的表现。"
    )


def _cross_check(
    frames: Mapping[SeriesKey, pd.DataFrame],
    experiments: Mapping[str, Any],
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-score the exported first-seed predictions and compare against DS-6 / DS-5b."""
    block = experiments.get("experiments", {})
    seeds = [str(seed) for seed in experiments.get("seeds", ())]
    declared_primary = experiments.get("primary_seed")
    if declared_primary is not None:
        first_seed = str(declared_primary)
        seed_source = "primary_seed"
    elif seeds:
        first_seed = seeds[0]
        seed_source = "seeds[0]"
    else:
        raise ValueError("交叉校验失败：DS-6 产物既没有 primary_seed 也没有 seeds，无法确定首种子")

    rows: list[dict[str, Any]] = []
    worst = 0.0
    for experiment in EXPERIMENTS:
        for stem in MODEL_STEMS:
            for horizon in HORIZONS:
                for segment in SEGMENTS:
                    scope = f"{experiment}/{stem}/h{horizon}/{segment}"
                    reported = (
                        block.get(experiment, {})
                        .get("models", {})
                        .get(stem, {})
                        .get(f"h{horizon}", {})
                        .get(segment, {})
                        .get("per_seed", {})
                        .get(first_seed)
                    )
                    key = SeriesKey(f"{stem}_{experiment}", horizon, segment)
                    if reported is None:
                        raise ValueError(
                            f"交叉校验失败：DS-6 产物缺少 {scope} 首种子 {first_seed} 的指标"
                        )
                    if key not in frames:
                        raise ValueError(f"交叉校验失败：缺少预测序列 {scope}")
                    mine = regression_cell(frames[key], horizon)
                    if int(mine["sample_count"]) != int(reported["sample_count"]):
                        raise ValueError(
                            f"交叉校验失败：{scope} 样本数不一致，"
                            f"复算 {mine['sample_count']}，DS-6 {reported['sample_count']}"
                        )
                    deltas = {
                        name: abs(float(mine[name]) - float(reported[name]))
                        for name in ("mae", "rmse", "r2", "nse")
                        if mine[name] is not None and reported.get(name) is not None
                    }
                    if not deltas:
                        raise ValueError(f"交叉校验失败：{scope} 没有可比较的指标字段")
                    if max(deltas.values()) > CROSS_CHECK_TOLERANCE:
                        raise ValueError(
                            f"交叉校验失败：{scope} 指标差异 {max(deltas.values()):.3e} "
                            f"超过允许误差 {CROSS_CHECK_TOLERANCE:.1e}"
                        )
                    worst = max(worst, *deltas.values())
                    rows.append(
                        {
                            "scope": scope,
                            "sample_count": mine["sample_count"],
                            "max_abs_difference": max(deltas.values()),
                        }
                    )

    baseline_rows: list[dict[str, Any]] = []
    baseline_worst = 0.0
    for segment in SEGMENTS:
        for horizon in HORIZONS:
            scope = f"persistence/h{horizon}/{segment}"
            reported = (
                baseline.get("metrics", {})
                .get(segment, {})
                .get("horizons", {})
                .get(str(horizon), {})
                .get("persistence")
            )
            key = SeriesKey(PERSISTENCE, horizon, segment)
            if reported is None:
                raise ValueError(f"交叉校验失败：DS-5b 产物缺少 {scope} 的指标")
            if key not in frames:
                raise ValueError(f"交叉校验失败：缺少持久性序列 {scope}")
            mine = regression_cell(frames[key], horizon)
            deltas = {
                name: abs(float(mine[name]) - float(reported[name]))
                for name in ("mae", "rmse", "r2", "nse")
                if mine[name] is not None and reported.get(name) is not None
            }
            if int(mine["sample_count"]) != int(reported["sample_count"]):
                raise ValueError(
                    f"交叉校验失败：{scope} 样本数不一致，"
                    f"复算 {mine['sample_count']}，DS-5b {reported['sample_count']}"
                )
            if not deltas:
                raise ValueError(
                    f"交叉校验失败：{scope} 没有可比较的指标字段（DS-5b 与复算值均为 null）"
                )
            if max(deltas.values()) > CROSS_CHECK_TOLERANCE:
                raise ValueError(
                    f"交叉校验失败：{scope} 指标差异 {max(deltas.values()):.3e} "
                    f"超过允许误差 {CROSS_CHECK_TOLERANCE:.1e}"
                )
            baseline_worst = max(baseline_worst, *deltas.values())
            baseline_rows.append(
                {
                    "scope": scope,
                    "sample_count": mine["sample_count"],
                    "max_abs_difference": max(deltas.values()),
                }
            )

    expected_cells = EXPECTED_PREDICTION_FILES
    if len(rows) != expected_cells or len(baseline_rows) != len(HORIZONS) * len(SEGMENTS):
        raise ValueError(
            "交叉校验失败：核对格子数不完整，"
            f"模型侧 {len(rows)} / {expected_cells}，持久性侧 "
            f"{len(baseline_rows)} / {len(HORIZONS) * len(SEGMENTS)}"
        )

    return {
        "scope": "本脚本按 DS-6 / DS-5b 产物中的首种子预测序列复算，再与它们自己报告的指标对比",
        "first_seed": first_seed,
        "seed_source": seed_source,
        "tolerance": CROSS_CHECK_TOLERANCE,
        "model_cells": {
            "count": len(rows),
            "max_abs_difference": worst,
            "rows": rows,
        },
        "persistence_cells": {
            "count": len(baseline_rows),
            "max_abs_difference": baseline_worst,
            "rows": baseline_rows,
        },
        "conclusion": (
            "交叉校验的保证范围：模型侧复算与 DS-6 报告的逐种子指标、持久性侧复算与 DS-5b 的 "
            "`hourly_baseline_metrics.json` 均**逐格一致**（最大绝对差见上，且不超过允许误差 "
            f"{CROSS_CHECK_TOLERANCE:.1e}），即**预测序列导出、样本行对齐与数值复现**三者一致；"
            "任一格子缺失、样本数不一致、指标字段缺失或差异超限都会直接失败。"
            "**本校验不能证明指标公式本身正确**：本脚本与 DS-6 / DS-5b 共用 "
            "`river_sentinel_ml.metrics.evaluate_forecasts`，公式若错三方会同步错、差值仍约为 0；"
            "指标公式的正确性由 `ml/tests/test_metrics.py` 的手算契约测试承担"
            "（MAE / RMSE / R² / NSE 的期望值均预先手算给出，不依赖实现本身）。"
        ),
    }


# --------------------------------------------------------------------------- #
# 渲染与落盘
# --------------------------------------------------------------------------- #


def render_metrics_json(payload: Mapping[str, Any]) -> str:
    """Strict JSON: non-finite values must already be ``null``."""
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def render_report(payload: Mapping[str, Any]) -> str:
    """Render the human-readable report (every number comes from ``payload``)."""
    declarations = payload["declarations"]
    threshold = payload["research_high_water"]["threshold"]
    focus = payload["focus_process"]
    imputation = payload["imputation"]
    lines: list[str] = [
        "# 小时误差分析：研究性高水位事件、峰值低估与滞后、确定性风险规则（DS-7）",
        "",
        "- 实验代号：DS-7；阈值名称统一为 **research_high_water（研究性高水位）**，"
        "**非官方标准、仅用于教学科研辅助**。",
        "- 生成方式：`python scripts/run_hourly_error_analysis.py`；本文件与 "
        "`data/processed/hourly_error_analysis.json` 由同一脚本**确定性生成**，禁止手工编辑。",
        "- 使用声明：本结果是**历史回放（离线）**数据上的**探索性 / 教学科研**结论，"
        "教学科研辅助，不替代官方防汛决策。",
        "",
        "## 1. 阈值来源与固定方式（research_high_water）",
        "",
        _table(
            ("项", "值"),
            (
                ("拟合位置", "**train 段**（validation / test 段不参与拟合、不重算）"),
                ("分位数", f"p{int(THRESHOLD_QUANTILE * 100)}（train 段有效观测）"),
                ("拟合样本数", str(payload["research_high_water"]["fit_rows"])),
                ("阈值", f"**{_number(threshold)} m**"),
                ("拟合口径", payload["research_high_water"]["fit_basis"]),
                ("单位", "m"),
            ),
        ),
        "",
        "> " + declarations["threshold_source"],
        "",
        "### 1.1 输入与来源证据",
        "",
        _table(
            ("输入", "文件", "SHA-256", "说明"),
            (
                (
                    "小时水位表",
                    f"`{payload['inputs']['hourly_levels']['name']}`",
                    f"`{payload['inputs']['hourly_levels']['sha256']}`",
                    f"{payload['inputs']['hourly_levels']['rows']} 行",
                ),
                (
                    "特征网格",
                    f"`{payload['inputs']['feature_grid']['name']}`",
                    f"`{payload['inputs']['feature_grid']['sha256']}`",
                    f"{payload['inputs']['feature_grid']['rows']} 行",
                ),
                (
                    "预测序列",
                    f"`{payload['inputs']['predictions']['name']}`",
                    "逐文件摘要",
                    f"{payload['inputs']['predictions']['files']} 个 CSV",
                ),
                (
                    "DS-6 指标",
                    f"`{payload['inputs']['model_experiments']['name']}`",
                    "—",
                    "、".join(payload["inputs"]["model_experiments"]["experiments"]),
                ),
                (
                    "DS-5b 基线",
                    f"`{payload['inputs']['baseline_metrics']['name']}`",
                    "—",
                    str(payload["inputs"]["baseline_metrics"]["schema_version"]),
                ),
            ),
        ),
        "",
        "### 1.2 网格上的研究性高水位事件（粗口径，用于核对事件规模）",
        "",
        _grid_table(payload["grid_event_summary"]),
        "",
        _grid_windows(payload["grid_event_summary"]),
        "",
        "> " + declarations["event_segment_convention"],
        "",
        "## 2. 逐段逐时域指标（对比口径）",
        "",
        "> " + declarations["support_difference"],
        "",
        _regression_table(payload, "test"),
        "",
        _regression_table(payload, "validation"),
        "",
        _regression_table(payload, "train"),
        "",
        "### 2.1 共同支持集上的重算（全部 7 类模型：持久性基线 + E1 / E2 的 Ridge、XGBoost、LSTM）",
        "",
        _common_table(payload),
        "",
        "### 2.2 与 DS-6 / DS-5b 产物的交叉校验",
        "",
        _cross_check_table(payload["cross_check"]),
        "",
        "## 3. 事件级：精确率 / 召回率 / F1",
        "",
        "> " + declarations["event_definition"],
        "",
        _event_table(payload["events"], "test", threshold),
        "",
        _event_table(payload["events"], "validation", threshold),
        "",
        "> " + declarations["validation_event_caveat"],
        "",
        "### 3.1 validation 连续高水位段内的逐段误差",
        "",
        "> " + declarations["segment_error"],
        "",
        _segment_error_table(payload["event_segment_errors"]),
        "",
        "> " + declarations["null_metrics"],
        "",
        "## 4. 峰值低估与滞后",
        "",
        "低估量 = 观测峰值时刻的（实测 − 预测），正值代表模型在该时刻**偏低**（最危险的方向）；"
        "滞后 = 预测峰值时刻 − 观测峰值时刻（小时），正值代表洪峰判晚。",
        "",
        _peak_table(payload["peaks"], "test"),
        "",
        _peak_table(payload["peaks"], "validation"),
        "",
        "## 5. 重点过程逐点核对：" + f"`{focus['window']['start']}` ~ `{focus['window']['end']}`",
        "",
        "> " + declarations["focus_window"],
        "",
        "窗口内实测**最大单小时变幅**："
        f"{_number(focus['max_change_in_window']['value'])} m"
        f"（目标时刻 `{focus['max_change_in_window']['at_target']}`，"
        f"方向：{focus['max_change_in_window']['direction']}）。",
        "",
        _process_table(focus["rows"]),
        "",
        "### 5.1 涨水段与退水段的平均误差",
        "",
        "> 统计窗口：`"
        + focus["phases"]["window"]["start"]
        + "` ~ `"
        + focus["phases"]["window"]["end"]
        + "`（与 §5 逐点表**同一窗口**，段 = "
        + focus["phases"]["window"]["segment"]
        + "）；窗口外的行不参与涨退水统计。",
        "",
        _phase_table(focus["phases"]),
        "",
        "## 6. 提前量（受 6 小时上限约束）",
        "",
        "> " + declarations["lead_definition"],
        "",
        _lead_table(payload["leads"]["validation"]),
        "",
        "## 7. 缺失特征填充（DS-6 方案 B）与由此产生的局限",
        "",
        "> " + declarations["imputation_policy"],
        "",
        "> " + declarations["imputation_note"],
        "",
        "### 7.1 E1 特征子集（冻结 "
        + str(imputation["experiments"]["E1"]["feature_count"])
        + " 列）",
        "",
        "> 特征列："
        + "、".join(f"`{column}`" for column in imputation["experiments"]["E1"]["feature_columns"])
        + "。",
        "",
        _imputation_table(imputation, "E1"),
        "",
        _COLUMN_NOTE,
        "",
        "### 7.2 E2 特征子集（冻结 "
        + str(imputation["experiments"]["E2"]["feature_count"])
        + " 列）",
        "",
        "> 特征列："
        + "、".join(f"`{column}`" for column in imputation["experiments"]["E2"]["feature_columns"])
        + "。",
        "",
        _imputation_table(imputation, "E2"),
        "",
        _COLUMN_NOTE,
        "",
        "> 填充统计交叉校验（本脚本按各实验冻结特征列在原始特征矩阵上复算 vs DS-6 产物）：共 "
        + str(imputation["cross_check"]["cells"])
        + " 个格子，最大绝对差 "
        + _difference(imputation["cross_check"]["max_abs_difference"])
        + "（该值为整数计数差，0 表示逐格完全一致）。",
        "",
        "> 填充统计与指标交叉校验的**保证范围**不同：填充侧是「DS-6 产物计数 vs 原始特征矩阵复算」"
        "两个不同来源的对照；指标侧的对照对象是 DS-6 / DS-5b 报告值，"
        "只能证明导出、行对齐与复现一致，指标公式的正确性由 `ml/tests/test_metrics.py` 的"
        "手算契约测试承担。",
        "",
        "## 8. p80 敏感性附录（只作参考，不改主口径）",
        "",
        "> " + declarations["sensitivity"],
        "",
        f"敏感性阈值：**{_number(payload['sensitivity_p80']['threshold'])} m**"
        f"（train 段 p{int(SENSITIVITY_QUANTILE * 100)}）。",
        "",
        _grid_table(payload["sensitivity_p80"]["grid_event_summary"]),
        "",
        _grid_windows(payload["sensitivity_p80"]["grid_event_summary"]),
        "",
        _sensitivity_event_table(payload["sensitivity_p80"]["events"]),
        "",
        "## 9. 结论保守性（HS-D-12）",
        "",
        declarations["conservatism"],
        "",
        "## 10. 使用限制",
        "",
        declarations["usage"],
        "",
        "## 11. 完整口径与限制声明（机器可读副本）",
        "",
    ]
    lines.extend(f"- **{name}**：{text}" for name, text in declarations.items())
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 渲染与序列化内部工具
# --------------------------------------------------------------------------- #


def _grid_table(summary: Mapping[str, Any]) -> str:
    return _table(
        ("段", "小时数", "有效观测", "段内最高实测", "事件点数", "事件段数"),
        [
            (
                segment,
                str(cell["hours"]),
                str(cell["usable_observations"]),
                _number(cell["max_level"]),
                str(cell["event_points"]),
                str(cell["event_segments"]),
            )
            for segment, cell in summary.items()
        ],
    )


def _grid_windows(summary: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for segment, cell in summary.items():
        if not cell["event_windows"]:
            lines.append(f"- **{segment}**：无研究性高水位事件（`null`）。")
            continue
        lines.append(f"- **{segment}**（{cell['event_segments']} 段）：")
        lines.extend(f"  - {window}" for window in cell["event_windows"])
    return "\n".join(lines)


def _regression_table(payload: Mapping[str, Any], segment: str) -> str:
    rows: list[tuple[str, ...]] = []
    for horizon in payload["horizons"]:
        for model in ALL_MODELS:
            cell = payload["regression"][segment][str(horizon)].get(model)
            if cell is None:
                continue
            rows.append(
                (
                    segment,
                    f"h{horizon}",
                    MODEL_LABELS[model],
                    str(cell["sample_count"]),
                    _number(cell["mae"]),
                    _number(cell["rmse"]),
                    _number(cell["r2"]),
                    _number(cell["nse"]),
                )
            )
    title = f"### {segment} 段（各模型各自的样本集）"
    header = ("段", "horizon", "模型", "sample_count", "MAE", "RMSE", "R²", "NSE")
    return title + "\n\n" + _table(header, rows)


def _common_table(payload: Mapping[str, Any]) -> str:
    rows: list[tuple[str, ...]] = []
    for segment in payload["segments"]:
        for horizon in payload["horizons"]:
            cell = payload["common_support"][segment][str(horizon)]
            for model in ALL_MODELS:
                metrics = cell["models"].get(model)
                if metrics is None:
                    continue
                rows.append(
                    (
                        segment,
                        f"h{horizon}",
                        MODEL_LABELS[model],
                        str(cell["sample_count"]),
                        _number(metrics["mae"]),
                        _number(metrics["rmse"]),
                        _number(metrics["r2"]),
                        _number(metrics["nse"]),
                    )
                )
    return _table(("段", "horizon", "模型", "共同样本数", "MAE", "RMSE", "R²", "NSE"), rows)


def _cross_check_table(cross_check: Mapping[str, Any]) -> str:
    models = cross_check["model_cells"]
    persistence = cross_check["persistence_cells"]
    rows: list[tuple[str, ...]] = []
    for entry in models["rows"]:
        rows.append(
            (
                entry["scope"],
                str(entry.get("sample_count", entry.get("measured"))),
                str(entry.get("reference_sample_count", entry.get("reference", "—"))),
                _difference(entry["max_abs_difference"])
                if "max_abs_difference" in entry
                else "样本数不一致",
            )
        )
    table = _table(
        ("范围", "复算样本数", "参考样本数", "逐指标最大绝对差"),
        rows,
    )
    return "\n".join(
        (
            f"- 校验范围：{cross_check['scope']}；首种子：`{cross_check['first_seed']}`"
            f"（取值来源：`{cross_check['seed_source']}`）。",
            f"- 模型侧：{models['count']} 个格子，逐指标最大绝对差 "
            f"**{_difference(models['max_abs_difference'])}**（科学计数法，真实值非 0）。",
            f"- 持久性侧（对 DS-5b）：{persistence['count']} 个格子，逐指标最大绝对差 "
            f"**{_difference(persistence['max_abs_difference'])}**。",
            f"- 结论：{cross_check['conclusion']}",
            "",
            table,
        )
    )


def _segment_error_table(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return f"validation 段在阈值下没有任何连续高水位事件段（{MISSING_TOKEN}），本表无法计算。"
    table_rows: list[tuple[str, ...]] = []
    for index, row in enumerate(rows, start=1):
        for model in ALL_MODELS:
            horizons = row["per_model"].get(model)
            if not horizons:
                continue
            for horizon in HORIZONS:
                cell = horizons.get(str(horizon))
                if cell is None:
                    continue
                table_rows.append(
                    (
                        f"段 {index}",
                        f"`{row['event_start']}`",
                        f"`{row['event_end']}`",
                        str(row["event_points"]),
                        MODEL_LABELS[model],
                        f"h{horizon}",
                        str(cell["sample_count"]),
                        _number(cell["mae"]),
                        _number(cell["observed_peak"]),
                        _number(cell["peak_prediction"]),
                        _number(cell["peak_underestimation"]),
                        str(cell["reason"] or MISSING_TOKEN),
                    )
                )
    return _table(
        (
            "事件段",
            "起点（目标时刻）",
            "终点（目标时刻）",
            "段内点数",
            "模型",
            "horizon",
            "段内样本数",
            "段内 MAE",
            "段内实测峰值",
            "峰值时刻预测",
            "峰值低估量",
            "reason",
        ),
        table_rows,
    )


def _event_table(events: Mapping[str, Any], segment: str, threshold: float) -> str:
    rows: list[tuple[str, ...]] = []
    for horizon in HORIZONS:
        for model in ALL_MODELS:
            cell = events[segment][str(horizon)].get(model)
            if cell is None:
                continue
            rows.append(
                (
                    segment,
                    f"h{horizon}",
                    MODEL_LABELS[model],
                    str(cell["observed_event_points"]),
                    str(cell["observed_event_segments"]),
                    str(cell["predicted_event_points"]),
                    _number(cell["precision"]),
                    _number(cell["recall"]),
                    _number(cell["f1"]),
                    str(cell["reason"] or MISSING_TOKEN),
                )
            )
    title = f"### {segment} 段事件级指标（阈值 {_number(threshold)} m）"
    header = (
        "段",
        "horizon",
        "模型",
        "观测事件点",
        "观测事件段",
        "预测事件点",
        "precision",
        "recall",
        "F1",
        "reason",
    )
    return title + "\n\n" + _table(header, rows)


def _peak_table(peaks: Mapping[str, Any], segment: str) -> str:
    rows: list[tuple[str, ...]] = []
    for horizon in HORIZONS:
        for model in ALL_MODELS:
            cell = peaks[segment][str(horizon)].get(model)
            if cell is None:
                continue
            rows.append(
                (
                    segment,
                    f"h{horizon}",
                    MODEL_LABELS[model],
                    _number(cell["observed_peak"]),
                    str(cell["observed_peak_time"] or MISSING_TOKEN),
                    _number(cell["prediction_at_peak"]),
                    _number(cell["underestimation"]),
                    _number(cell["max_absolute_error"]),
                    str(cell["max_absolute_error_time"] or MISSING_TOKEN),
                    str(cell["max_absolute_error_direction"] or MISSING_TOKEN),
                    MISSING_TOKEN
                    if cell["peak_lag_hours"] is None
                    else str(cell["peak_lag_hours"]),
                )
            )
    header = (
        "段",
        "horizon",
        "模型",
        "观测峰值",
        "峰值时刻",
        "峰值时刻预测",
        "低估量",
        "最大绝对误差",
        "误差最大时刻",
        "方向",
        "滞后（小时）",
    )
    return f"### {segment} 段\n\n" + _table(header, rows)


def _process_table(rows: Mapping[str, Any]) -> str:
    rendered: list[tuple[str, ...]] = []
    for name in sorted(rows):
        for entry in rows[name]:
            rendered.append(
                (
                    name,
                    str(entry["target"]),
                    str(entry["anchor"]),
                    _number(entry["observed"]),
                    _number(entry["prediction"]),
                    _number(entry["absolute_error"]),
                    entry["direction"],
                )
            )
    header = ("模型_horizon", "目标时刻", "锚点", "实测", "预测", "绝对误差", "方向")
    return _table(header, rendered)


def _phase_table(phases: Mapping[str, Any]) -> str:
    rows: list[tuple[str, ...]] = []
    for model in ALL_MODELS:
        for horizon in HORIZONS:
            cell = phases["models"].get(model, {}).get(str(horizon))
            if cell is None:
                continue
            rows.append(
                (
                    MODEL_LABELS[model],
                    f"h{horizon}",
                    str(cell[RISING_KEY]["sample_count"]),
                    _number(cell[RISING_KEY]["mae"]),
                    _number(cell[RISING_KEY]["mean_signed_error"]),
                    str(cell[FALLING_KEY]["sample_count"]),
                    _number(cell[FALLING_KEY]["mae"]),
                    _number(cell[FALLING_KEY]["mean_signed_error"]),
                )
            )
    header = (
        "模型",
        "horizon",
        "涨水样本",
        "涨水 MAE",
        "涨水平均有符号误差",
        "退水样本",
        "退水 MAE",
        "退水平均有符号误差",
    )
    return _table(header, rows)


def _lead_table(leads: Mapping[str, Any]) -> str:
    rows: list[tuple[str, ...]] = []
    for horizon in HORIZONS:
        for model in ALL_MODELS:
            for entry in leads[str(horizon)].get(model, []):
                rows.append(
                    (
                        f"h{horizon}",
                        MODEL_LABELS[model],
                        str(entry["event_start"]),
                        str(entry["event_end"]),
                        str(entry["event_points"]),
                        MISSING_TOKEN if entry["lead_hours"] is None else str(entry["lead_hours"]),
                        str(entry["first_issue_time"] or MISSING_TOKEN),
                        _number(entry["first_prediction"]),
                        str(entry["reason"] or MISSING_TOKEN),
                    )
                )
    header = (
        "horizon",
        "模型",
        "事件开始",
        "事件结束",
        "事件点数",
        "提前量（小时）",
        "首次触及阈值的发布时间",
        "该行预测值",
        "reason",
    )
    return _table(header, rows)


def _imputation_table(profile: Mapping[str, Any], experiment: str) -> str:
    """Missing / imputed accounting of one experiment, per segment / horizon / model."""
    block = profile["experiments"][experiment]
    rows: list[tuple[str, ...]] = []
    for segment in SEGMENTS:
        for horizon in HORIZONS:
            for stem in MODEL_STEMS:
                model = f"{stem}_{experiment}"
                cell = block["cells"][segment][f"h{horizon}"][model]
                rows.append(
                    (
                        segment,
                        f"h{horizon}",
                        MODEL_LABELS[model],
                        str(cell["usable_rows"]),
                        str(cell["rows_with_missing_features"]),
                        str(cell["missing_feature_cells"]),
                        str(cell["input_cells"]),
                        str(cell["imputed_rows"]),
                        str(cell["imputed_cells"]),
                        _percent(cell["imputed_cell_share"]),
                        str(cell["missing_handling_label"]),
                    )
                )
    header = (
        "段",
        "horizon",
        "模型",
        "样本数",
        "含缺失特征的行数",
        "缺失特征单元格数",
        "输入单元格数",
        "填充行数",
        "填充单元格数",
        "填充/输入",
        "缺失处理",
    )
    return _table(header, rows)


def _percent(share: float | None) -> str:
    return MISSING_TOKEN if share is None else f"{float(share) * 100:.2f}%"


def _difference(value: float | None) -> str:
    """Render a tolerance-scale difference without hiding small magnitudes."""
    if value is None:
        return MISSING_TOKEN
    number = float(value)
    return "0" if number == 0.0 else f"{number:.3e}"


def _sensitivity_event_table(events: Mapping[str, Any]) -> str:
    rows: list[tuple[str, ...]] = []
    for segment in sorted(events):
        for horizon in HORIZONS:
            for model in sorted(events[segment][str(horizon)]):
                cell = events[segment][str(horizon)][model]
                rows.append(
                    (
                        segment,
                        f"h{horizon}",
                        MODEL_LABELS[model],
                        str(cell["observed_event_points"]),
                        str(cell["observed_event_segments"]),
                        _number(cell["precision"]),
                        _number(cell["recall"]),
                        _number(cell["f1"]),
                        str(cell["reason"] or MISSING_TOKEN),
                    )
                )
    header = (
        "段",
        "horizon",
        "模型",
        "观测事件点",
        "观测事件段",
        "precision",
        "recall",
        "F1",
        "reason",
    )
    return _table(header, rows)


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _window_text(segment: Any) -> str:
    if segment.start_time is None or segment.end_time is None:
        return f"[{segment.start_index}, {segment.end_index}]"
    return f"`{_moment(segment.start_time)}`~`{_moment(segment.end_time)}`（{segment.length} 点）"


def _moment(value: object) -> str | None:
    return None if value is None else value.isoformat()  # type: ignore[union-attr]


def _number(value: float | None) -> str:
    return MISSING_TOKEN if value is None else f"{value:.6f}"


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _direction(residual: float) -> str:
    if residual < 0.0:
        return "under"
    if residual > 0.0:
        return "over"
    return "exact"


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 of one file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_artifacts(
    payload: Mapping[str, Any],
    *,
    metrics_path: Path,
    report_path: Path,
) -> tuple[Path, Path]:
    """Write the JSON evidence and the report (UTF-8, LF)."""
    metrics_destination = Path(metrics_path)
    report_destination = Path(report_path)
    _write_text(metrics_destination, render_metrics_json(payload))
    _write_text(report_destination, render_report(payload))
    return metrics_destination.resolve(), report_destination.resolve()


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def main(argv: list[str] | None = None) -> int:
    """End-to-end entry point."""
    parser = argparse.ArgumentParser(description="DS-7 误差分析与事件 / 峰值 / 提前量证据生成。")
    parser.add_argument("--hourly", default=str(DEFAULT_HOURLY_CSV), help="小时水位表")
    parser.add_argument("--features", default=str(DEFAULT_FEATURE_CSV), help="小时特征网格")
    parser.add_argument(
        "--predictions", default=str(DEFAULT_PREDICTIONS_DIR), help="DS-6 预测序列目录"
    )
    parser.add_argument(
        "--experiments", default=str(DEFAULT_EXPERIMENTS_JSON), help="DS-6 实验指标 JSON"
    )
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE_JSON), help="DS-5b 基线 JSON")
    parser.add_argument(
        "--output-json", default=str(DEFAULT_OUTPUT_JSON), help="指标 JSON 落盘路径"
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT_MD), help="人读报告落盘路径")
    args = parser.parse_args(argv)

    payload = run_analysis(
        hourly_source=Path(args.hourly),
        grid_source=Path(args.features),
        predictions_dir=Path(args.predictions),
        experiments_source=Path(args.experiments),
        baseline_source=Path(args.baseline),
    )
    metrics_path, report_path = write_artifacts(
        payload, metrics_path=Path(args.output_json), report_path=Path(args.report)
    )

    research = payload["research_high_water"]
    print("THRESHOLD", research["threshold"], "FIT_ROWS", research["fit_rows"])
    for segment in SEGMENTS:
        summary = payload["grid_event_summary"][segment]
        print(
            "GRID",
            segment,
            "usable",
            summary["usable_observations"],
            "max",
            _number(summary["max_level"]),
            "events",
            summary["event_points"],
            "segments",
            summary["event_segments"],
        )
    for segment in SEGMENTS:
        for horizon in payload["horizons"]:
            for model in ALL_MODELS:
                cell = payload["regression"][segment][str(horizon)].get(model)
                if cell is None:
                    continue
                print(
                    "METRICS",
                    segment,
                    f"h{horizon}",
                    model,
                    "n",
                    cell["sample_count"],
                    "MAE",
                    _number(cell["mae"]),
                    "RMSE",
                    _number(cell["rmse"]),
                    "R2",
                    _number(cell["r2"]),
                    "NSE",
                    _number(cell["nse"]),
                )
    for segment in ("validation", "test"):
        for horizon in payload["horizons"]:
            for model in ALL_MODELS:
                cell = payload["events"][segment][str(horizon)][model]
                print(
                    "EVENTS",
                    segment,
                    f"h{horizon}",
                    model,
                    "points",
                    cell["observed_event_points"],
                    "P",
                    _number(cell["precision"]),
                    "R",
                    _number(cell["recall"]),
                    "F1",
                    _number(cell["f1"]),
                    "reason",
                    cell["reason"] or MISSING_TOKEN,
                )
    for experiment, block in payload["imputation"]["experiments"].items():
        for segment in SEGMENTS:
            for horizon in payload["horizons"]:
                for stem in MODEL_STEMS:
                    model = f"{stem}_{experiment}"
                    cell = block["cells"][segment][f"h{horizon}"][model]
                    print(
                        "IMPUTATION",
                        experiment,
                        model,
                        segment,
                        f"h{horizon}",
                        "rows",
                        cell["usable_rows"],
                        "rows_with_missing_features",
                        cell["rows_with_missing_features"],
                        "missing_cells",
                        cell["missing_feature_cells"],
                        "input_cells",
                        cell["input_cells"],
                        "imputed_rows",
                        cell["imputed_rows"],
                        "imputed_cells",
                        cell["imputed_cells"],
                    )
    phases = payload["focus_process"]["phases"]
    print(
        "PHASE_WINDOW",
        phases["window"]["start"],
        phases["window"]["end"],
        phases["window"]["segment"],
    )
    print("IMPUTATION_CROSS_CHECK", payload["imputation"]["cross_check"]["max_abs_difference"])
    print("SENSITIVITY_P80", payload["sensitivity_p80"]["threshold"])
    print("METRICS_JSON", metrics_path)
    print("REPORT", report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

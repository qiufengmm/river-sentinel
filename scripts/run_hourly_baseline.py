"""DS-5b 小时基线实验：未来 1 / 3 / 6 小时的持久性与参考下界。

本脚本只做**基线**：不训练模型、不做特征工程、不接降雨。它的作用是给 DS-6 提供对照下限，
并给出目标序列在真实数据上的可预测性量级。

流程（顺序固定）：

1. 读入 DS-2b 交付的小时表 ``observed_at, water_level_m, is_imputed, quality_flag,
   source_record_id``（每行一小时、时间戳含 ``+08:00``）；
2. 在**完整序列**上一次性生成持久性预测 ``prediction_h{h}(t) = water_level_m(t-h)``
   （直接多时域，禁止递归滚动，D-1）；
3. 按 ``0.70 / 0.15 / 0.15`` 时间顺序切分（HS-D-7），**再按段切片**；
4. 另用 **train 段**水位均值构造常数参考下界（HS-D-8：只由 train 段拟合，段内不重算），
   并与持久性基线在**完全相同**的样本集上评估；
5. 每段每 horizon 剔除「目标时刻 ``is_imputed=True``」的样本（C-5）后，
   复用 ``metrics.evaluate_forecasts`` 计算 MAE / RMSE / R² / NSE / 峰值绝对误差；
6. 写出严格 JSON 证据与确定性的人读报告。

口径与限制：

- 桶语义（HS-D-5）：小时表每个桶取**桶内最后一次观测**、时间戳为**桶左边界**，因此标签 ``t``
  的取值可能晚于 ``t`` 至多 1 小时，**1 小时 horizon 的指标偏乐观**，3 / 6 小时不受影响；
  该序列不是严格因果序列。
- 无有效配对样本时写 ``sample_count=0`` 与四项指标 ``null`` 并给出 ``reason``，不填 0、不中断。
- 产物不含绝对路径、不含运行时间戳，同一输入两次运行逐字节一致。

用法::

    python scripts/run_hourly_baseline.py
    python scripts/run_hourly_baseline.py --input <小时表> --report <报告路径>
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
from pandas.api.types import is_bool_dtype, is_numeric_dtype

from river_sentinel_ml.baseline import persistence_forecast
from river_sentinel_ml.dataset import chronological_split
from river_sentinel_ml.metrics import ForecastMetrics, evaluate_forecasts

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_CSV = REPO_ROOT / "data" / "processed" / "water_level_hourly_cata12720.csv"
DEFAULT_METRICS_JSON = REPO_ROOT / "data" / "processed" / "hourly_baseline_metrics.json"
DEFAULT_REPORT_MD = REPO_ROOT / "docs" / "experiments" / "hourly-baseline-1-3-6.md"

SCHEMA_VERSION = "ds5b-hourly-baseline-1"
HORIZONS: tuple[int, ...] = (1, 3, 6)
TRAIN_RATIO = 0.70
VALIDATION_RATIO = 0.15
FREQUENCY = "1h"
#: DS-2b-R1 由主 Agent 裁定的插补阈值（HS-D-6 已闭合），本脚本只记录、不重算。
EXPECTED_MAX_INTERPOLATION_STEPS = 2

SEGMENT_NAMES: tuple[str, ...] = ("train", "validation", "test")
SEGMENT_ROLES: dict[str, str] = {
    "train": "calibration-fit",
    "validation": "held-out",
    "test": "held-out",
}
MODEL_PERSISTENCE = "persistence"
MODEL_TRAIN_MEAN = "train_mean"
BASELINE_NAMES: tuple[str, ...] = (MODEL_PERSISTENCE, MODEL_TRAIN_MEAN)

NO_VALID_PAIRS_REASON = "no_valid_pairs"
REQUIRED_COLUMNS: tuple[str, ...] = ("observed_at", "water_level_m", "is_imputed")
HOURLY_COLUMNS: tuple[str, ...] = (
    "observed_at",
    "water_level_m",
    "is_imputed",
    "quality_flag",
    "source_record_id",
)
PREDICTION_PREFIX = "prediction_h"
MISSING_TOKEN = "null"

#: DS-5b 提示词 §3.6 给出的全表口径预期值（对照用，**不得**为对齐而调整实际口径）。
CROSS_CHECK_REFERENCE: dict[str, dict[str, int]] = {
    "without_imputation_exclusion": {"h1": 1955, "h3": 1807, "h6": 1613},
    "with_imputation_exclusion": {"h1": 1954, "h3": 1806, "h6": 1612},
}

PERSISTENCE_DEFINITION = (
    "prediction_h{h}(t) = water_level_m(t - h)：以当前小时水位为 anchor 的直接多时域预测，"
    "禁止递归滚动（HS-D-3 / HS-D-4）；anchor 时刻被插补的样本不被剔除，被剔除的是**目标时刻**。"
)
TRAIN_MEAN_DEFINITION = "以 train 段水位均值为常数预测，train / validation / test 三段共用同一常数，段内不重算（HS-D-8）。"

STATIC_DECLARATIONS: dict[str, str] = {
    "step_unit": (
        "步长口径（HS-D-1）：1 / 3 / 6 是 1 小时网格上的步数，等价于未来 1 / 3 / 6 小时；"
        "列名沿用 prediction_h{h}。"
    ),
    "bounded_look_ahead": (
        "桶语义与有界前瞻（HS-D-5）：小时表每个桶取【桶内最后一次观测】、时间戳为【桶左边界】，"
        "因此标签 t 的取值可能晚于 t 至多 1 小时，**1 小时 horizon 的指标偏乐观**，3 / 6 小时不受该影响；"
        "该序列不是严格因果序列，报告与论文都不得如此描述。"
    ),
    "target_exclusion": (
        "C-5 剔除规则：任何被评分样本都必须同时满足 observed 非缺失、prediction_h{h} 非缺失"
        "且**目标时刻** is_imputed=False；被剔除的样本单独计入 excluded_imputed，不填 0、不跳过该格。"
    ),
    "sample_set_alignment": (
        "公平对比口径：训练集均值参考下界与持久性基线在**完全相同**的样本集上评估（同段、同 horizon、"
        "同剔除规则），因此两者的 sample_count 与 excluded_imputed 逐格相等；参考下界不享受更大样本集。"
    ),
    "train_mean_calibration": (
        "参考下界校准：常数只由 train 段 is_imputed=False 且 water_level_m 有限的观测拟合，"
        "不使用 validation / test 段统计量（HS-D-8）；train 段自身的指标因此是该常数的样本内结果，"
        "不是泛化结果。"
    ),
    "split": (
        "切分与段首口径（HS-D-7 / D-1）：按时间顺序 0.70 / 0.15 / 0.15，不 shuffle、段间不重叠；"
        "预测在完整序列上一次性生成后按段切片，因此 validation / test 段首若干行的 anchor 可来自上一段末尾，"
        "绝不在段内独立 shift。"
    ),
    "imputation_threshold": (
        "插补阈值：max_interpolation_steps=2（主 Agent 裁定，HS-D-6 已闭合），本脚本只记录该阈值，"
        "不重新插补；被插补行 is_imputed=True 且 quality_flag='imputed'。"
    ),
    "no_rainfall": (
        "本轮不含降雨：只使用历史水位一个变量，DS-3 / DS-3b 的小时网格化再分析降雨未参与，"
        "水雨特征增益待 DS-6 评估。"
    ),
    "conservatism": (
        "结论保守性（HS-D-12）：全部数字与结论都是**探索性 / 教学科研**结果，不得作为正式洪水预警，"
        "不得就高水位事件下确定性结论，也不得给出「超警」判断。"
    ),
    "null_metrics": (
        "空值口径：某段某 horizon 的有效配对样本为 0 时，写 sample_count=0、四项指标与峰值绝对误差写 null，"
        "并给出 reason=no_valid_pairs；不填 0、不跳过该格、不中断脚本。目标方差为 0 时（既有实现口径）"
        "R² 与 NSE 写 null。"
    ),
    "known_limitations": (
        "已知限制：R² / NSE 在目标方差为 0 时按既有实现返回 null；validation（463 h）与 test（464 h）"
        "两段很短，指标方差大；train 段指标是参考下界的样本内结果；1 小时 horizon 受有界前瞻影响偏乐观；"
        "本实验不含降雨、不含模型、不含超参搜索。"
    ),
    "usage": (
        "使用限制：数据为历史回放 / 离线数据，不代表实时水情；本报告仅用于教学、科研与辅助分析，"
        "教学科研辅助，不替代官方防汛决策。"
    ),
}


@dataclass(frozen=True)
class HorizonEvaluation:
    """One segment / one horizon evaluation under the C-5 exclusion rule."""

    sample_count: int
    excluded_imputed: int
    metrics: ForecastMetrics | None
    reason: str | None


# --------------------------------------------------------------------------- #
# 输入校验与预测构造
# --------------------------------------------------------------------------- #


def validate_hourly_frame(frame: object) -> pd.DataFrame:
    """校验小时表契约并返回同一对象（不复制、不修改）。

    Raises:
        TypeError: ``frame`` 不是 :class:`pandas.DataFrame`。
        ValueError: 缺少 ``observed_at`` / ``water_level_m`` / ``is_imputed`` 之一，
            ``observed_at`` 不是带时区的 datetime、含缺失值、有重复或不严格递增，
            ``water_level_m`` 不是数值，或 ``is_imputed`` 不是布尔。
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"frame must be a pandas DataFrame, got {type(frame).__name__}")

    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"frame must contain the columns {', '.join(missing)}")

    stamps = frame["observed_at"]
    if not isinstance(stamps.dtype, pd.DatetimeTZDtype):
        raise ValueError(
            "frame['observed_at'] must be timezone-aware datetime data, "
            f"got {stamps.dtype!r}; naive timestamps make the hourly grid ambiguous"
        )
    if bool(stamps.isna().any()):
        raise ValueError("frame['observed_at'] must not contain missing timestamps")
    if not bool(stamps.is_unique):
        raise ValueError("frame['observed_at'] must not contain duplicate timestamps")
    if not bool(stamps.is_monotonic_increasing):
        raise ValueError("frame['observed_at'] must be monotonically increasing in time")

    if not is_numeric_dtype(frame["water_level_m"]):
        raise ValueError(
            f"frame['water_level_m'] must be numeric, got {frame['water_level_m'].dtype!r}"
        )
    if not is_bool_dtype(frame["is_imputed"]):
        raise ValueError(f"frame['is_imputed'] must be boolean, got {frame['is_imputed'].dtype!r}")
    return frame


def build_forecast_frame(
    frame: pd.DataFrame,
    horizons: Sequence[int] = HORIZONS,
) -> pd.DataFrame:
    """在**完整序列**上生成持久性预测，返回带 ``observed_at`` 的预测帧。

    返回列固定为 ``observed_at, observed, prediction_h{h}..., is_imputed``，行与输入一一对应。
    输入帧不被修改（包括 dtype）。
    """
    validated = validate_hourly_frame(frame)
    steps = tuple(int(horizon) for horizon in horizons)
    series = pd.Series(
        validated["water_level_m"].to_numpy(dtype="float64"),
        index=pd.DatetimeIndex(validated["observed_at"]),
        name="water_level_m",
    )
    forecast = persistence_forecast(series, steps)
    forecast.insert(0, "observed_at", pd.DatetimeIndex(validated["observed_at"]))
    forecast["is_imputed"] = validated["is_imputed"].to_numpy(dtype=bool)
    return forecast


def split_hourly_frame(
    frame: pd.DataFrame,
    *,
    train_ratio: float = TRAIN_RATIO,
    validation_ratio: float = VALIDATION_RATIO,
) -> dict[str, pd.DataFrame]:
    """按时间顺序切分，返回键序固定为 ``train / validation / test`` 的字典。"""
    split = chronological_split(frame, train_ratio=train_ratio, validation_ratio=validation_ratio)
    return {"train": split.train, "validation": split.validation, "test": split.test}


def train_mean_value(train_frame: pd.DataFrame) -> float:
    """只由 train 段拟合参考下界常数（HS-D-8）。

    入参是 :func:`build_forecast_frame` 的 train 段切片：水位标签在 ``observed`` 列中。

    Raises:
        ValueError: train 段没有 ``is_imputed=False`` 且水位有限的观测，常数无从拟合。
    """
    levels = train_frame["observed"].to_numpy(dtype="float64")
    usable = np.isfinite(levels) & ~train_frame["is_imputed"].to_numpy(dtype=bool)
    if not bool(usable.any()):
        raise ValueError(
            "train segment has no finite observation with is_imputed=False; "
            "the reference lower bound cannot be calibrated"
        )
    return float(np.mean(levels[usable]))


def constant_prediction_frame(
    frame: pd.DataFrame,
    horizons: Sequence[int] = HORIZONS,
    value: float = 0.0,
) -> pd.DataFrame:
    """把参考下界写成与持久性**同样本集**的预测列。

    只在持久性预测有限的位置写入常数，其余位置保持缺失：这样参考下界与持久性基线的
    有效配对样本逐格相同，两者的指标可直接比较。
    """
    result = frame.copy()
    for horizon in horizons:
        column = f"{PREDICTION_PREFIX}{int(horizon)}"
        if column not in result.columns:
            raise ValueError(f"frame must contain a '{column}' column")
        finite = result[column].notna().to_numpy()
        result[column] = np.where(finite, float(value), np.nan)
    return result


# --------------------------------------------------------------------------- #
# 评估
# --------------------------------------------------------------------------- #


def evaluate_horizon(frame: pd.DataFrame, horizon: int) -> HorizonEvaluation:
    """按 C-5 剔除后评估单个 horizon；无有效样本时返回空值而非抛错。"""
    column = f"{PREDICTION_PREFIX}{int(horizon)}"
    for required in ("observed", column):
        if required not in frame.columns:
            raise ValueError(f"frame must contain a '{required}' column")

    observed = frame["observed"].to_numpy(dtype="float64")
    prediction = frame[column].to_numpy(dtype="float64")
    imputed = frame["is_imputed"].to_numpy(dtype=bool)
    paired = np.isfinite(observed) & np.isfinite(prediction)
    excluded_imputed = int(np.count_nonzero(paired & imputed))
    keep = paired & ~imputed
    if not bool(keep.any()):
        return HorizonEvaluation(
            sample_count=0,
            excluded_imputed=excluded_imputed,
            metrics=None,
            reason=NO_VALID_PAIRS_REASON,
        )

    scored = frame.loc[keep, ["observed", column]]
    metrics = evaluate_forecasts(scored, (int(horizon),))[int(horizon)]
    return HorizonEvaluation(
        sample_count=metrics.sample_count,
        excluded_imputed=excluded_imputed,
        metrics=metrics,
        reason=None,
    )


# --------------------------------------------------------------------------- #
# 端到端证据
# --------------------------------------------------------------------------- #


def run_hourly_baseline(
    frame: pd.DataFrame,
    *,
    source_sha256: str,
    horizons: Sequence[int] = HORIZONS,
    train_ratio: float = TRAIN_RATIO,
    validation_ratio: float = VALIDATION_RATIO,
    max_interpolation_steps: int = EXPECTED_MAX_INTERPOLATION_STEPS,
    source_name: str = "water_level_hourly_cata12720.csv",
) -> dict[str, Any]:
    """跑完整条基线链路，返回严格 JSON 可序列化的证据字典（不写盘、不含绝对路径）。"""
    validated = validate_hourly_frame(frame)
    steps = tuple(int(horizon) for horizon in horizons)
    forecast = build_forecast_frame(validated, steps)
    segments = split_hourly_frame(
        forecast, train_ratio=train_ratio, validation_ratio=validation_ratio
    )
    train_mean = train_mean_value(segments["train"])

    segment_payloads: list[dict[str, Any]] = []
    metrics_payload: dict[str, Any] = {}
    for name in SEGMENT_NAMES:
        part = segments[name]
        mean_part = constant_prediction_frame(part, steps, train_mean)
        horizons_payload: dict[str, Any] = {}
        for horizon in steps:
            horizons_payload[str(horizon)] = {
                MODEL_PERSISTENCE: _evaluation_payload(evaluate_horizon(part, horizon)),
                MODEL_TRAIN_MEAN: _evaluation_payload(evaluate_horizon(mean_part, horizon)),
            }
        missing = int(part["observed"].isna().sum())
        hours = int(len(part))
        metrics_payload[name] = {
            "role": SEGMENT_ROLES[name],
            "hours": hours,
            "first_at": _moment(part["observed_at"].iloc[0]),
            "last_at": _moment(part["observed_at"].iloc[-1]),
            "missing_buckets": missing,
            "missing_rate": round(missing / hours, 6) if hours else None,
            "imputed_rows": int(part["is_imputed"].to_numpy(dtype=bool).sum()),
            "horizons": horizons_payload,
        }
        segment_payloads.append(
            {
                "name": name,
                "role": SEGMENT_ROLES[name],
                "hours": hours,
                "first_at": _moment(part["observed_at"].iloc[0]),
                "last_at": _moment(part["observed_at"].iloc[-1]),
                "missing_buckets": missing,
                "missing_rate": round(missing / hours, 6) if hours else None,
            }
        )

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "horizons": [int(horizon) for horizon in steps],
        "source": {
            "name": source_name,
            "sha256": source_sha256,
            "rows": int(len(validated)),
            "columns": list(validated.columns),
            "frequency": FREQUENCY,
            "max_interpolation_steps": int(max_interpolation_steps),
            "imputed_rows": int(validated["is_imputed"].to_numpy(dtype=bool).sum()),
            "first_at": _moment(validated["observed_at"].iloc[0]),
            "last_at": _moment(validated["observed_at"].iloc[-1]),
        },
        "split": {
            "train_ratio": float(train_ratio),
            "validation_ratio": float(validation_ratio),
            "segment_order": list(SEGMENT_NAMES),
            "segments": segment_payloads,
        },
        "metrics": metrics_payload,
        "baselines": {
            MODEL_PERSISTENCE: {
                "definition": PERSISTENCE_DEFINITION,
                "randomness": "确定性基线，无随机性、无随机种子",
            },
            MODEL_TRAIN_MEAN: {
                "definition": TRAIN_MEAN_DEFINITION,
                "value": _finite(train_mean),
                "calibration_rows": int(
                    np.count_nonzero(
                        np.isfinite(segments["train"]["observed"].to_numpy(dtype="float64"))
                        & ~segments["train"]["is_imputed"].to_numpy(dtype=bool)
                    )
                ),
                "calibration_basis": "train 段 is_imputed=False 且 water_level_m 有限的观测",
                "evaluation_scope": (
                    "与持久性基线在同一段、同一 horizon、同一剔除规则下的完全相同样本集上评估"
                ),
            },
        },
        "cross_check": _cross_check(forecast, steps, validated),
    }
    payload["declarations"] = {
        **STATIC_DECLARATIONS,
        "test_segment_limitation": _test_segment_limitation(payload),
    }
    return payload


def _evaluation_payload(evaluation: HorizonEvaluation) -> dict[str, Any]:
    """把一个 horizon 的评估结果写成 JSON 单元格（空值写 null）。"""
    metrics = evaluation.metrics
    return {
        "sample_count": int(evaluation.sample_count),
        "excluded_imputed": int(evaluation.excluded_imputed),
        "mae": None if metrics is None else _finite(metrics.mae),
        "rmse": None if metrics is None else _finite(metrics.rmse),
        "r2": None if metrics is None else _finite(metrics.r2),
        "nse": None if metrics is None else _finite(metrics.nse),
        "peak_absolute_error": None if metrics is None else _finite(metrics.peak_absolute_error),
        "reason": evaluation.reason,
    }


def _cross_check(
    forecast: pd.DataFrame,
    horizons: Sequence[int],
    validated: pd.DataFrame,
) -> dict[str, Any]:
    """全表（不分段）可用样本数与提示词预期值对照，差额照实报告。"""
    observed = forecast["observed"].to_numpy(dtype="float64")
    imputed = forecast["is_imputed"].to_numpy(dtype=bool)
    without: dict[str, int] = {}
    with_exclusion: dict[str, int] = {}
    excluded: dict[str, int] = {}
    for horizon in horizons:
        prediction = forecast[f"{PREDICTION_PREFIX}{int(horizon)}"].to_numpy(dtype="float64")
        paired = np.isfinite(observed) & np.isfinite(prediction)
        without[f"h{horizon}"] = int(np.count_nonzero(paired))
        with_exclusion[f"h{horizon}"] = int(np.count_nonzero(paired & ~imputed))
        excluded[f"h{horizon}"] = int(np.count_nonzero(paired & imputed))

    difference = {
        key: with_exclusion[key] - CROSS_CHECK_REFERENCE["with_imputation_exclusion"][key]
        for key in with_exclusion
    }
    aligned = all(value == 0 for value in difference.values())
    conclusion = (
        "全表口径实测与提示词预期逐项一致（不剔除插补 "
        + _join_counts(without)
        + "；剔除插补后 "
        + _join_counts(with_exclusion)
        + "）。"
        if aligned
        else (
            "全表口径实测与提示词预期**不一致**：剔除插补后实测 "
            + _join_counts(with_exclusion)
            + "，预期 "
            + _join_counts(CROSS_CHECK_REFERENCE["with_imputation_exclusion"])
            + "，差额 "
            + _join_counts(difference, signed=True)
            + "。差额来自「目标时刻被插补」的样本数（"
            + _join_counts(excluded)
            + "）："
            + f"全表共 {int(imputed.sum())} 行被插补，每行是否落在有效配对位置上逐 horizon 不同，"
            "本轮**未**为对齐预期调整任何口径。"
        )
    )
    return {
        "scope": "全表（不分段）",
        "reference_expected": CROSS_CHECK_REFERENCE,
        "measured": {
            "without_imputation_exclusion": without,
            "with_imputation_exclusion": with_exclusion,
        },
        "difference": difference,
        "excluded_imputed_by_horizon": excluded,
        "imputed_rows_total": int(validated["is_imputed"].to_numpy(dtype=bool).sum()),
        "imputed_rows_at": [
            _moment(stamp)
            for stamp in validated.loc[validated["is_imputed"].to_numpy(dtype=bool), "observed_at"]
        ],
        "conclusion": conclusion,
    }


def _test_segment_limitation(payload: Mapping[str, Any]) -> str:
    """用实测值生成 test 段限制声明（缺失率、各 horizon 有效样本）。"""
    test = payload["metrics"][SEGMENT_NAMES[-1]]
    counts = " / ".join(
        str(test["horizons"][str(horizon)][MODEL_PERSISTENCE]["sample_count"])
        for horizon in payload["horizons"]
    )
    rate = test["missing_rate"]
    rendered = "null" if rate is None else f"{rate:.2%}"
    return (
        f"test 段限制：{test['hours']} 小时中缺失 {test['missing_buckets']} 个桶"
        f"（缺失率 {rendered}），h1 / h3 / h6 有效配对样本 {counts}；该缺失来自**数据源本身**"
        "（DS-2b 实测的长断线缺口），不是模型或切分缺陷；据此不得就高水位事件下确定性结论，"
        "也不得给出正式洪水预警或「超警」判断（HS-D-12）。"
    )


# --------------------------------------------------------------------------- #
# 渲染与落盘
# --------------------------------------------------------------------------- #


def render_metrics_json(payload: Mapping[str, Any]) -> str:
    """严格 JSON：非有限值必须已写成 ``null``，``allow_nan=False`` 作为兜底。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def render_report(payload: Mapping[str, Any]) -> str:
    """渲染人读报告；全部数字来自 ``payload``，不含运行时间戳与绝对路径。"""
    source = payload["source"]
    declarations = payload["declarations"]
    cross = payload["cross_check"]
    lines: list[str] = [
        "# 小时基线实验：未来 1 / 3 / 6 小时水位预测（持久性 + 训练集均值参考下界）",
        "",
        "- 实验代号：DS-5b（小时尺度基线，不含模型、不含特征工程、不含降雨）",
        "- 生成方式：`python scripts/run_hourly_baseline.py`；本文件与 "
        "`data/processed/hourly_baseline_metrics.json` 由同一脚本**确定性生成**，禁止手工编辑。",
        "- 使用声明：本实验是**历史回放（离线）**数据上的**探索性 / 教学科研**结果，"
        "教学科研辅助，不替代官方防汛决策。",
        "",
        "## 1. 数据与段边界（含每段缺失率）",
        "",
        "### 1.1 输入",
        "",
        _table(
            ("项", "值"),
            (
                ("输入文件", f"`{source['name']}`"),
                ("SHA-256", f"`{source['sha256']}`"),
                ("行数", str(source["rows"])),
                ("重采样频率", f"`{source['frequency']}`"),
                ("max_interpolation_steps", str(source["max_interpolation_steps"])),
                ("插补行数（is_imputed=True）", str(source["imputed_rows"])),
                ("首次 / 末次观测", f"`{source['first_at']}` / `{source['last_at']}`"),
            ),
        ),
        "",
        "### 1.2 段边界与缺失率（0.70 / 0.15 / 0.15，按时序、不 shuffle）",
        "",
        _table(
            ("段", "角色", "小时数", "起始", "结束", "缺失桶", "缺失率", "插补行数"),
            [
                (
                    segment["name"],
                    segment["role"],
                    str(segment["hours"]),
                    f"`{segment['first_at']}`",
                    f"`{segment['last_at']}`",
                    str(segment["missing_buckets"]),
                    _rate(segment["missing_rate"]),
                    str(payload["metrics"][segment["name"]]["imputed_rows"]),
                )
                for segment in payload["split"]["segments"]
            ],
        ),
        "",
        "### 1.3 全表（不分段）口径交叉核对",
        "",
        _table(
            (
                "horizon",
                "实测（不剔除插补）",
                "实测（剔除插补后）",
                "提示词预期（剔除插补后）",
                "差额",
            ),
            [
                (
                    f"h{horizon}",
                    str(cross["measured"]["without_imputation_exclusion"][f"h{horizon}"]),
                    str(cross["measured"]["with_imputation_exclusion"][f"h{horizon}"]),
                    str(cross["reference_expected"]["with_imputation_exclusion"][f"h{horizon}"]),
                    f"{cross['difference'][f'h{horizon}']:+d}",
                )
                for horizon in payload["horizons"]
            ],
        ),
        "",
        cross["conclusion"],
        "",
        f"全表插补行数：{cross['imputed_rows_total']}；插补时刻："
        + "、".join(f"`{stamp}`" for stamp in cross["imputed_rows_at"])
        + "；各 horizon 因「目标时刻被插补」而剔除的样本数："
        + _join_counts(cross["excluded_imputed_by_horizon"])
        + "。",
        "",
        "## 2. 指标：三段 × 三 horizon",
        "",
        "### 2.1 样本量与剔除（C-5：目标时刻 is_imputed=True 的样本被剔除）",
        "",
        _table(
            ("段", "horizon", "sample_count", "excluded_imputed"),
            [
                (
                    name,
                    f"h{horizon}",
                    str(
                        payload["metrics"][name]["horizons"][str(horizon)][MODEL_PERSISTENCE][
                            "sample_count"
                        ]
                    ),
                    str(
                        payload["metrics"][name]["horizons"][str(horizon)][MODEL_PERSISTENCE][
                            "excluded_imputed"
                        ]
                    ),
                )
                for name in SEGMENT_NAMES
                for horizon in payload["horizons"]
            ],
        ),
        "",
        "### 2.2 持久性基线（prediction_h{h}(t) = water_level_m(t - h)）",
        "",
        _metric_table(payload, MODEL_PERSISTENCE),
        "",
        "### 2.3 训练集均值参考下界（常数预测，与持久性同样本集）",
        "",
        _metric_table(payload, MODEL_TRAIN_MEAN),
        "",
        f"校准值：**{_number(payload['baselines'][MODEL_TRAIN_MEAN]['value'])}**（"
        f"calibration_rows = {payload['baselines'][MODEL_TRAIN_MEAN]['calibration_rows']}；"
        f"口径：{payload['baselines'][MODEL_TRAIN_MEAN]['calibration_basis']}；"
        f"{payload['baselines'][MODEL_TRAIN_MEAN]['evaluation_scope']}）。",
        "",
        "持久性与参考下界的 MAE / RMSE 差额即为持久性基线在该格上的增益；train 段是参考下界的"
        "样本内结果，不能当作泛化性能。",
        "",
        "## 3. HS-D-5 桶语义与有界前瞻声明",
        "",
        declarations["bounded_look_ahead"],
        "",
        "## 4. test 段限制声明",
        "",
        declarations["test_segment_limitation"],
        "",
        "## 5. 结论保守性（HS-D-12）",
        "",
        declarations["conservatism"],
        "",
        "## 6. 本轮不含降雨",
        "",
        declarations["no_rainfall"],
        "",
        "## 7. 已知限制",
        "",
        declarations["known_limitations"],
        "",
        "## 8. 完整口径与限制声明（机器可读副本）",
        "",
    ]
    lines.extend(f"- **{name}**：{text}" for name, text in declarations.items())
    lines.append("")
    return "\n".join(lines)


def write_artifacts(
    payload: Mapping[str, Any],
    *,
    metrics_path: Path,
    report_path: Path,
) -> tuple[Path, Path]:
    """落盘 JSON 与报告（UTF-8、LF、父目录自动创建），返回两个绝对路径。"""
    metrics_destination = Path(metrics_path)
    report_destination = Path(report_path)
    _write_text(metrics_destination, render_metrics_json(payload))
    _write_text(report_destination, render_report(payload))
    return metrics_destination.resolve(), report_destination.resolve()


def load_hourly_frame(path: Path) -> pd.DataFrame:
    """读取小时表 CSV 并把 ``observed_at`` 解析成带时区的 datetime。"""
    frame = pd.read_csv(path)
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"{path} is missing the columns {', '.join(missing)}")
    if not isinstance(frame["observed_at"].dtype, pd.DatetimeTZDtype):
        frame["observed_at"] = pd.to_datetime(frame["observed_at"], format="ISO8601")
    return validate_hourly_frame(frame)


def sha256_of_file(path: Path) -> str:
    """流式计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    """端到端入口：读小时表 → 三段 × 三 horizon 指标 → 落盘 JSON 与报告。"""
    parser = argparse.ArgumentParser(
        description="DS-5b 小时基线实验：持久性 + 训练集均值参考下界（1 / 3 / 6 步）。"
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT_CSV), help="DS-2b 交付的小时表 CSV")
    parser.add_argument(
        "--metrics-json", default=str(DEFAULT_METRICS_JSON), help="指标 JSON 落盘路径"
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT_MD), help="人读报告落盘路径")
    parser.add_argument(
        "--max-interpolation-steps",
        type=int,
        default=EXPECTED_MAX_INTERPOLATION_STEPS,
        help="小时表使用的插补阈值（只记录，不重新插补）",
    )
    args = parser.parse_args(argv)

    source = Path(args.input)
    frame = load_hourly_frame(source)
    payload = run_hourly_baseline(
        frame,
        source_sha256=sha256_of_file(source),
        max_interpolation_steps=args.max_interpolation_steps,
        source_name=source.name,
    )
    metrics_path, report_path = write_artifacts(
        payload, metrics_path=Path(args.metrics_json), report_path=Path(args.report)
    )

    print("INPUT", source)
    print("SOURCE_SHA256", payload["source"]["sha256"])
    print(
        "ROWS",
        payload["source"]["rows"],
        "IMPUTED",
        payload["source"]["imputed_rows"],
        "MAX_INTERPOLATION_STEPS",
        payload["source"]["max_interpolation_steps"],
    )
    print("TRAIN_MEAN", payload["baselines"][MODEL_TRAIN_MEAN]["value"])
    for name in SEGMENT_NAMES:
        segment = payload["metrics"][name]
        print(
            "SEGMENT",
            name,
            segment["role"],
            "hours",
            segment["hours"],
            "missing",
            segment["missing_buckets"],
            "missing_rate",
            segment["missing_rate"],
        )
        for horizon in payload["horizons"]:
            cell = segment["horizons"][str(horizon)]
            print(
                "  h" + str(horizon),
                "n",
                cell[MODEL_PERSISTENCE]["sample_count"],
                "excluded_imputed",
                cell[MODEL_PERSISTENCE]["excluded_imputed"],
                "MAE",
                _number(cell[MODEL_PERSISTENCE]["mae"]),
                "RMSE",
                _number(cell[MODEL_PERSISTENCE]["rmse"]),
                "R2",
                _number(cell[MODEL_PERSISTENCE]["r2"]),
                "NSE",
                _number(cell[MODEL_PERSISTENCE]["nse"]),
                "| train_mean MAE",
                _number(cell[MODEL_TRAIN_MEAN]["mae"]),
            )
    print("METRICS_JSON", metrics_path)
    print("REPORT", report_path)
    return 0


# --------------------------------------------------------------------------- #
# 渲染与序列化内部工具
# --------------------------------------------------------------------------- #


def _metric_table(payload: Mapping[str, Any], baseline: str) -> str:
    """渲染某个基线在三段 × 三 horizon 上的完整指标表。"""
    return _table(
        ("段", "horizon", "sample_count", "MAE", "RMSE", "R²", "NSE", "峰值绝对误差", "reason"),
        [
            (
                name,
                f"h{horizon}",
                str(payload["metrics"][name]["horizons"][str(horizon)][baseline]["sample_count"]),
                _number(payload["metrics"][name]["horizons"][str(horizon)][baseline]["mae"]),
                _number(payload["metrics"][name]["horizons"][str(horizon)][baseline]["rmse"]),
                _number(payload["metrics"][name]["horizons"][str(horizon)][baseline]["r2"]),
                _number(payload["metrics"][name]["horizons"][str(horizon)][baseline]["nse"]),
                _number(
                    payload["metrics"][name]["horizons"][str(horizon)][baseline][
                        "peak_absolute_error"
                    ]
                ),
                str(
                    payload["metrics"][name]["horizons"][str(horizon)][baseline]["reason"]
                    or MISSING_TOKEN
                ),
            )
            for name in SEGMENT_NAMES
            for horizon in payload["horizons"]
        ],
    )


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _number(value: float | None) -> str:
    return MISSING_TOKEN if value is None else f"{value:.6f}"


def _rate(value: float | None) -> str:
    return MISSING_TOKEN if value is None else f"{value:.2%}"


def _join_counts(counts: Mapping[str, int], *, signed: bool = False) -> str:
    return " / ".join(
        f"{key} {value:+d}" if signed else f"{key} {value}" for key, value in counts.items()
    )


def _moment(value: Any) -> str:
    return value.isoformat()


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _write_text(path: Path, text: str) -> None:
    """UTF-8 + LF 写入（``newline="\\n"`` 避免 Windows 换行转换影响确定性）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


if __name__ == "__main__":
    raise SystemExit(main())

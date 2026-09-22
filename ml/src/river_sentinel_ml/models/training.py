"""统一训练/评估入口、特征子集选择与确定性种子（DS-6）。

本模块把 DS-6 的**实验协议**固化成代码，而不是留在提示词或报告里：

- **两组特征集**：``E1`` 只用历史水位（9 列），``E2`` 用水位 + 网格化再分析降雨（20 列）。
  两组的列名与顺序都由 :data:`WATER_ONLY_FEATURES` / :data:`FULL_FEATURES` 冻结，
  并以 :func:`select_features` 强制校验（缺列直接 ``ValueError``，不做静默降级）。
- **直接多时域**：每个 ``horizon`` 单独训练一个模型（HS-D-3），不存在递归滚动预测。
- **只由 train 拟合、test 只用一次**：``alpha`` 选择与 LSTM/XGBoost 早停只看
  ``validation``；``test`` 只在最终评估时读一次。
- **多随机种子**：默认 ``(20260921, 1, 2)``，报告均值 ± 标准差。
- **零样本兜底**：某段某 horizon 一个有效样本都没有时写 ``sample_count=0``、
  五项指标为 ``null``、附 ``reason``；不填 0、不崩溃。
- **指标口径**：直接复用 :func:`river_sentinel_ml.metrics.evaluate_forecasts`，
  不另写一套公式。

本模块不读写文件、不访问网络；除各模型自身的随机性外没有额外随机源。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from numbers import Integral
from typing import Any

import numpy as np
import pandas as pd

from ..metrics import evaluate_forecasts
from .gbdt import fit_xgboost
from .linear import DEFAULT_ALPHAS, fit_ridge, select_alpha
from .sequence import fit_lstm
from .splitting import (
    SEGMENT_ORDER,
    SegmentSamples,
    build_all_segments,
    validated_horizon,
)

#: E1：只用历史水位 + 时钟特征。列名与顺序冻结，不得增删或重排。
WATER_ONLY_FEATURES: tuple[str, ...] = (
    "water_level_t",
    "water_level_lag_1",
    "water_level_lag_2",
    "water_level_lag_3",
    "water_level_lag_6",
    "water_level_lag_12",
    "water_level_lag_24",
    "hour_of_day",
    "month",
)

#: E2 相对 E1 多出来的 11 列降雨特征（6 个瞬时滞后 + 5 个累计窗口）。
RAIN_FEATURES: tuple[str, ...] = (
    "rain_lag_0",
    "rain_lag_1",
    "rain_lag_2",
    "rain_lag_3",
    "rain_lag_6",
    "rain_lag_12",
    "rain_cum_1",
    "rain_cum_3",
    "rain_cum_6",
    "rain_cum_12",
    "rain_cum_24",
)

#: E2：水位 + 降雨，共 20 列，顺序与 ``features.feature_columns()`` 完全一致。
FULL_FEATURES: tuple[str, ...] = WATER_ONLY_FEATURES[:7] + RAIN_FEATURES + ("hour_of_day", "month")

FEATURE_SUBSETS: Mapping[str, tuple[str, ...]] = {
    "E1": WATER_ONLY_FEATURES,
    "E2": FULL_FEATURES,
}

#: 预测步数（小时）。1 小时网格下"步"与"小时"等价（HS-D-1）。
HORIZONS: tuple[int, ...] = (1, 3, 6)

#: 多随机种子：首个为运行日期，便于与实验记录对齐。
SEEDS: tuple[int, ...] = (20260921, 1, 2)

#: 参与建模的模型名（持久性基线直接引用 DS-5b 实测值，不在此重训）。
MODEL_NAMES: tuple[str, ...] = ("ridge", "xgboost", "lstm")

#: 需要聚合的指标名，顺序固定。
METRIC_NAMES: tuple[str, ...] = ("mae", "rmse", "r2", "nse", "peak_absolute_error")

#: 零样本时的固定说明，保证产物里"没有数值"与"数值为 0"可以区分。
ZERO_SAMPLE_REASON = "no usable sample: the segment has no target-valid row for this horizon"
NO_PAIRWISE_REASON = "no pairwise-valid sample: every prediction or target is missing"


# --------------------------------------------------------------------------- #
# Metric wrappers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MetricSummary:
    """One (segment, horizon, model, seed) metric row; ``None`` means "not computable"."""

    sample_count: int
    mae: float | None = None
    rmse: float | None = None
    r2: float | None = None
    nse: float | None = None
    peak_absolute_error: float | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Flatten to a JSON-shaped dict with ``None`` instead of non-finite values."""
        payload: dict[str, Any] = {"sample_count": int(self.sample_count)}
        for name in METRIC_NAMES:
            value = getattr(self, name)
            payload[name] = None if value is None else _finite_or_none(float(value))
        payload["reason"] = self.reason if self.sample_count == 0 else None
        return payload


def evaluate_predictions(observed: object, predicted: object, *, horizon: int) -> MetricSummary:
    """Evaluate one horizon with the frozen metric implementation.

    Args:
        observed: 观测值序列（``target_h{h}``）。
        predicted: 同长度的预测值序列；缺失值与观测缺失值一样被成对剔除。
        horizon: 预测步数，正整数。

    Returns:
        :class:`MetricSummary`。有效样本为 0 时返回 ``sample_count=0``、五项指标
        ``None`` 与 ``reason``，**不抛错**（DS-6 零样本兜底）。

    Raises:
        ValueError: ``horizon`` 非正整数；两个序列长度不一致。
    """
    steps = validated_horizon(horizon)
    target = np.asarray(observed, dtype="float64").reshape(-1)
    forecast = np.asarray(predicted, dtype="float64").reshape(-1)
    if target.size != forecast.size:
        raise ValueError(
            f"observed and predicted must have the same length, got {target.size} and {forecast.size}"
        )
    if target.size == 0:
        return MetricSummary(sample_count=0, reason=ZERO_SAMPLE_REASON)

    frame = pd.DataFrame({"observed": target, f"prediction_h{steps}": forecast})
    try:
        reference = evaluate_forecasts(frame, horizons=(steps,))[steps]
    except ValueError:
        return MetricSummary(sample_count=0, reason=NO_PAIRWISE_REASON)
    return MetricSummary(
        sample_count=int(reference.sample_count),
        mae=float(reference.mae),
        rmse=float(reference.rmse),
        r2=None if reference.r2 is None else float(reference.r2),
        nse=None if reference.nse is None else float(reference.nse),
        peak_absolute_error=float(reference.peak_absolute_error),
        reason=None,
    )


def aggregate_metric_summaries(summaries: Sequence[MetricSummary]) -> dict[str, Any]:
    """Aggregate per-seed summaries into ``mean ± std`` per metric.

    Args:
        summaries: 同一 (experiment, model, horizon, segment) 在各随机种子下的结果；
            非空，且应全部来自同一段同一 horizon。

    Returns:
        ``{"sample_count", "sample_counts_by_seed", "reason", <metric>: {"mean", "std"}}``。
        某项指标在所有种子上都不可计算时写 ``{"mean": None, "std": None}``，不写 0。

    Raises:
        ValueError: ``summaries`` 为空。
    """
    if not summaries:
        raise ValueError("summaries must not be empty")

    aggregate: dict[str, Any] = {
        "sample_count": int(summaries[0].sample_count),
        "sample_counts_by_seed": [int(summary.sample_count) for summary in summaries],
    }
    reasons = [summary.reason for summary in summaries if summary.reason]
    aggregate["reason"] = "; ".join(dict.fromkeys(reasons)) if reasons else None
    for name in METRIC_NAMES:
        values = [
            value
            for value in (getattr(summary, name) for summary in summaries)
            if value is not None and math.isfinite(float(value))
        ]
        if values:
            array = np.asarray(values, dtype="float64")
            aggregate[name] = {"mean": float(array.mean()), "std": float(array.std())}
        else:
            aggregate[name] = {"mean": None, "std": None}
    return aggregate


def to_json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats with ``None`` and numpy scalars with Python ones."""
    if isinstance(value, Mapping):
        return {str(key): to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_safe(item) for item in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return _finite_or_none(float(value))
    if isinstance(value, np.ndarray):
        return [to_json_safe(item) for item in value.tolist()]
    return value


def _finite_or_none(value: float) -> float | None:
    """Return ``value`` when finite, otherwise ``None`` (strict JSON has no NaN/Infinity)."""
    return value if math.isfinite(value) else None


# --------------------------------------------------------------------------- #
# Feature subsets
# --------------------------------------------------------------------------- #


def select_features(frame: pd.DataFrame, experiment: str) -> tuple[str, ...]:
    """Return the frozen feature columns of one experiment, validating the frame.

    Raises:
        ValueError: ``experiment`` 不是已知的实验名，或 ``frame`` 缺少该实验的某个特征列。
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"frame must be a pandas DataFrame, got {type(frame).__name__}")
    if experiment not in FEATURE_SUBSETS:
        raise ValueError(
            f"unknown experiment {experiment!r}; known experiments: {sorted(FEATURE_SUBSETS)}"
        )
    columns = FEATURE_SUBSETS[experiment]
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise ValueError(
            f"frame is missing the feature column(s) {', '.join(missing)} "
            f"required by experiment {experiment}"
        )
    return columns


# --------------------------------------------------------------------------- #
# Experiment runner
# --------------------------------------------------------------------------- #

#: 训练回调签名：``(experiment, model, horizon, seed, forecaster)``。
ArtifactWriter = Callable[[str, str, int, int, object], None]
#: 预测回调签名：``(experiment, model, horizon, seed, segment, frame)``。
PredictionWriter = Callable[[str, str, int, int, str, pd.DataFrame], None]


def run_hourly_experiments(
    frame: pd.DataFrame,
    *,
    experiments: Sequence[str] = tuple(FEATURE_SUBSETS),
    horizons: Sequence[int] = HORIZONS,
    seeds: Sequence[int] = SEEDS,
    model_names: Sequence[str] = MODEL_NAMES,
    prediction_writer: PredictionWriter | None = None,
    artifact_writer: ArtifactWriter | None = None,
) -> dict[str, Any]:
    """Train every requested model/horizon/experiment and evaluate all three segments.

    Args:
        frame: DS-4b 特征矩阵。
        experiments: 参与实验的特征集名称（``E1`` / ``E2``）。
        horizons: 预测步数。
        seeds: 随机种子列表，非空。
        model_names: :data:`MODEL_NAMES` 的子集。
        prediction_writer: 可选的逐 (种子, 段) 回调，拿到 ``observed_at/segment/observed/
            prediction`` 四列，用于落盘可复现的预测序列。
        artifact_writer: 可选的训练完成回调，拿到已拟合的模型对象。

    Returns:
        与 :func:`to_json_safe` 兼容的嵌套字典：
        ``{"horizons", "seeds", "models", "experiments": {名称: {"feature_columns",
        "models": {模型: {"h{h}": {"train"/"validation"/"test": {...}, "hyperparameters": {...}}}}}}}``。

    Raises:
        TypeError: ``frame`` 不是 DataFrame。
        ValueError: 任一实验名、模型名、horizon 或种子非法。
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"frame must be a pandas DataFrame, got {type(frame).__name__}")

    experiment_names = _validated_names(experiments, known=FEATURE_SUBSETS, name="experiments")
    models = _validated_names(model_names, known=set(MODEL_NAMES), name="model_names")
    steps = [validated_horizon(value) for value in horizons]
    if not steps:
        raise ValueError("horizons must not be empty")
    seed_values = _validated_seeds(seeds)

    payload: dict[str, Any] = {
        "horizons": steps,
        "seeds": seed_values,
        "models": models,
        "experiments": {},
    }
    for experiment in experiment_names:
        columns = select_features(frame, experiment)
        models_payload: dict[str, Any] = {}
        for model_name in models:
            per_horizon: dict[str, Any] = {}
            for horizon in steps:
                segments = build_all_segments(frame, horizon=horizon, feature_columns=columns)
                per_horizon[f"h{horizon}"] = _run_one(
                    experiment=experiment,
                    model_name=model_name,
                    horizon=horizon,
                    segments=segments,
                    seeds=seed_values,
                    prediction_writer=prediction_writer,
                    artifact_writer=artifact_writer,
                )
            models_payload[model_name] = per_horizon
        payload["experiments"][experiment] = {
            "feature_columns": list(columns),
            "models": models_payload,
        }
    return payload


def _run_one(
    *,
    experiment: str,
    model_name: str,
    horizon: int,
    segments: Mapping[str, SegmentSamples],
    seeds: Sequence[int],
    prediction_writer: PredictionWriter | None,
    artifact_writer: ArtifactWriter | None,
) -> dict[str, Any]:
    """Train one (experiment, model, horizon) across seeds and evaluate three segments."""
    train = segments["train"]
    validation = segments["validation"]
    alpha: float | None = None
    alpha_scores: dict[str, float | None] | None = None
    if model_name == "ridge":
        alpha, alpha_scores = select_alpha(train, validation, alphas=DEFAULT_ALPHAS)

    per_seed: dict[str, dict[str, Any]] = {}
    summaries: dict[str, list[MetricSummary]] = {name: [] for name in SEGMENT_ORDER}
    hyperparameters: dict[str, Any] = {"seeds": list(seeds)}
    if alpha is not None:
        hyperparameters["alpha"] = alpha
        hyperparameters["alpha_validation_mae"] = alpha_scores

    for seed in seeds:
        forecaster, detail = _train_one(model_name, train, validation, seed=seed, alpha=alpha)
        hyperparameters[str(seed)] = detail
        if artifact_writer is not None:
            artifact_writer(experiment, model_name, horizon, seed, forecaster)

        seed_entry: dict[str, Any] = {}
        for segment_name in SEGMENT_ORDER:
            samples = segments[segment_name]
            predictions = np.asarray(forecaster.predict(samples.as_matrix()), dtype="float64")
            summary = evaluate_predictions(samples.target, predictions, horizon=horizon)
            summaries[segment_name].append(summary)
            seed_entry[segment_name] = summary.as_dict()
            if prediction_writer is not None:
                prediction_writer(
                    experiment,
                    model_name,
                    horizon,
                    seed,
                    segment_name,
                    pd.DataFrame(
                        {
                            "observed_at": samples.observed_at,
                            "segment": segment_name,
                            "observed": samples.target,
                            "prediction": predictions,
                        }
                    ),
                )
        per_seed[str(seed)] = seed_entry

    entry: dict[str, Any] = {}
    for segment_name in SEGMENT_ORDER:
        samples = segments[segment_name]
        aggregate = aggregate_metric_summaries(summaries[segment_name])
        aggregate["usable_rows"] = int(samples.usable_rows)
        aggregate["dropped_missing_features"] = int(samples.missing_feature_rows)
        aggregate["per_seed"] = {str(seed): per_seed[str(seed)][segment_name] for seed in seeds}
        entry[segment_name] = aggregate
    entry["hyperparameters"] = hyperparameters
    return entry


def _train_one(
    model_name: str,
    train: SegmentSamples,
    validation: SegmentSamples,
    *,
    seed: int,
    alpha: float | None,
) -> tuple[Any, dict[str, Any]]:
    """Fit one model and return ``(forecaster, hyper-parameter record)``."""
    if model_name == "ridge":
        assert alpha is not None  # 由 `_run_one` 在同一次调用里先选好
        forecaster = fit_ridge(train, alpha=alpha)
        return forecaster, {
            "alpha": alpha,
            "fit_rows": int(train.rows - forecaster.dropped_rows),
            "dropped_rows": int(forecaster.dropped_rows),
        }
    if model_name == "xgboost":
        forecaster = fit_xgboost(train, validation, seed=seed)
        return forecaster, {
            "params": {
                key: value
                for key, value in forecaster.params.items()
                if key not in {"verbosity", "objective", "eval_metric", "tree_method", "device"}
            },
            "best_iteration": forecaster.best_iteration,
            "dropped_rows": 0,
        }
    if model_name == "lstm":
        forecaster = fit_lstm(train, validation, seed=seed)
        return forecaster, {
            "hidden_size": forecaster.hidden_size,
            "epochs_max": 300,
            "best_epoch": forecaster.best_epoch,
            "epochs_run": forecaster.epochs_run,
            "best_validation_loss": forecaster.best_validation_loss,
            "device": forecaster.device,
            "dropped_rows": int(forecaster.dropped_rows),
        }
    raise ValueError(f"unknown model {model_name!r}; known models: {sorted(MODEL_NAMES)}")


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _validated_names(values: Iterable[str], *, known: Iterable[Any], name: str) -> list[str]:
    """Coerce to a non-empty list of known names, preserving the caller's order."""
    items = [str(value) for value in values]
    if not items:
        raise ValueError(f"{name} must not be empty")
    allowed = set(known)
    unknown = [value for value in items if value not in allowed]
    if unknown:
        raise ValueError(f"unknown {name} {', '.join(unknown)}; known values: {sorted(allowed)}")
    return items


def _validated_seeds(values: Iterable[int]) -> list[int]:
    """Coerce the seed list to unique non-negative integers."""
    items: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, Integral):
            raise ValueError(f"seeds must be integers, got {value!r}")
        items.append(int(value))
    if not items:
        raise ValueError("seeds must not be empty")
    if len(set(items)) != len(items):
        raise ValueError(f"seeds must not contain duplicates, got {tuple(items)!r}")
    return items

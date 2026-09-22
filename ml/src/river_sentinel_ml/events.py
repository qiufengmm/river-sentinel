"""Research high-water events, event-level metrics and peak error diagnostics.

术语口径（严格小时尺度，不得退回日尺度措辞）：

- 水位主序列是 ``water_level_t``（小时网格值，``observed_at`` 为桶左沿）；
- 预测时域为**未来 1 / 3 / 6 步**，1 小时网格下等价于 1 / 3 / 6 小时；
- 提前量以**小时**计，最大 6 小时。

``research_high_water``（研究性高水位）是本教学科研项目自造的**研究性阈值**：
它由 **train 段**的分位数拟合得出，validation / test 段**不得参与也不得重算**。
它不是官方警戒水位、不是超警或正式预警标准，任何页面、报告与智能体回答都不得
将其表述为官方标准，并必须标注「非官方标准，仅用于教学科研辅助」。

本模块是纯函数集合：不读文件、不访问网络、不写盘、不修改入参、无随机性，
同一输入的两次调用结果完全一致。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Real

import numpy as np
import pandas as pd

#: 研究性阈值的统一名称（页面、报告、代码、docstring 必须一致）。
RESEARCH_HIGH_WATER = "research_high_water"

#: 阈值与事件口径的必带声明。
RESEARCH_HIGH_WATER_NOTE = (
    "research_high_water 为研究性阈值（train 段分位数拟合），非官方标准，"
    "仅用于教学科研辅助，不替代官方防汛决策。"
)

TRAIN_SEGMENT = "train"
LEVEL_COLUMN = "water_level_t"
SEGMENT_COLUMN = "segment"
IMPUTED_COLUMN = "is_imputed_t"
FIT_REQUIRED_COLUMNS: tuple[str, ...] = (SEGMENT_COLUMN, LEVEL_COLUMN, IMPUTED_COLUMN)

NO_OBSERVED_EVENTS = "no_observed_events"
NO_PREDICTED_EVENTS = "no_predicted_events"
NO_ADVANCE_REASON = "crossing_after_event_start"
NO_CROSSING_REASON = "no_prediction_above_threshold"

DIRECTION_UNDER = "under"
DIRECTION_OVER = "over"
DIRECTION_NONE = "none"


@dataclass(frozen=True)
class EventSegment:
    """One run of consecutive labelled time steps.

    Attributes:
        start_index: Position of the first labelled step inside the evaluated mask.
        end_index: Position of the last labelled step (inclusive).
        start_time: Timestamp at ``start_index`` when timestamps are supplied.
        end_time: Timestamp at ``end_index`` when timestamps are supplied.
    """

    start_index: int
    end_index: int
    start_time: pd.Timestamp | None = None
    end_time: pd.Timestamp | None = None

    @property
    def length(self) -> int:
        """Number of steps in the run."""
        return int(self.end_index - self.start_index + 1)


@dataclass(frozen=True)
class EventMetrics:
    """Point-level detection metrics for research high-water events.

    观测事件点数为 ``0`` 时事件级指标**无法计算**：``precision`` / ``recall`` / ``f1``
    全部为 ``None`` 且 ``reason`` 为 :data:`NO_OBSERVED_EVENTS`；禁止用 ``0`` 冒充，
    也不得拿这些格子参与任何"谁更好"的比较。
    """

    precision: float | None
    recall: float | None
    f1: float | None
    tp: int
    fp: int
    fn: int
    observed_event_points: int
    observed_event_segments: int
    predicted_event_points: int
    predicted_event_segments: int
    reason: str | None


@dataclass(frozen=True)
class PeakError:
    """Peak-level error diagnostics for one forecast series.

    ``underestimation`` 是观测峰值时刻的低估量 ``observed - prediction``：正值表示
    模型在该时刻给出偏低的预测（防汛上最危险的误差方向），负值表示偏高。
    """

    observed_peak: float | None
    observed_peak_time: pd.Timestamp | None
    prediction_at_peak: float | None
    underestimation: float | None
    max_absolute_error: float | None
    max_absolute_error_time: pd.Timestamp | None
    max_absolute_error_direction: str | None


@dataclass(frozen=True)
class EarlyLead:
    """Earliest advance warning for one event segment, expressed in hours."""

    lead_hours: int | None
    issue_time: pd.Timestamp | None
    target_time: pd.Timestamp | None
    predicted_value: float | None
    reason: str | None


# --------------------------------------------------------------------------- #
# 阈值拟合
# --------------------------------------------------------------------------- #


def fit_research_high_water_threshold(
    frame: pd.DataFrame,
    *,
    quantile: float = 0.90,
) -> float:
    """Fit the research high-water threshold on the **train segment only**.

    Args:
        frame: Feature grid holding ``segment`` / ``water_level_t`` / ``is_imputed_t``.
        quantile: Quantile in ``(0, 1]`` applied to the usable train observations.

    Returns:
        The fitted threshold in metres.

    Raises:
        TypeError: ``frame`` 不是 :class:`pandas.DataFrame`。
        ValueError: 缺少必需列、``quantile`` 不在 ``(0, 1]``，或 train 段没有任何
            非缺失且未被插补的水位观测。

    有效观测 = ``water_level_t`` 有限且 ``is_imputed_t`` 为 ``False`` 且 ``segment``
    为 ``train``；validation / test 段不参与拟合也不参与重算（防止事后调阈值）。
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"frame must be a pandas DataFrame, got {type(frame).__name__}")

    missing = [column for column in FIT_REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"frame must contain the columns {', '.join(missing)}")

    share = _validated_quantile(quantile)
    trains = frame[SEGMENT_COLUMN].astype(str) == TRAIN_SEGMENT
    levels = frame.loc[trains, LEVEL_COLUMN].to_numpy(dtype="float64")
    imputed = frame.loc[trains, IMPUTED_COLUMN].to_numpy(dtype=bool)
    usable = levels[np.isfinite(levels) & ~imputed]

    if usable.size == 0:
        raise ValueError(
            f"the {TRAIN_SEGMENT!r} segment has no usable observation "
            f"(finite {LEVEL_COLUMN} with {IMPUTED_COLUMN}=False); the "
            f"{RESEARCH_HIGH_WATER} threshold cannot be fitted"
        )
    return float(np.quantile(usable, share))


def label_research_high_water(levels: Sequence[float], threshold: float) -> np.ndarray:
    """Label water levels as research high-water events (``>=`` the threshold).

    Args:
        levels: Water-level series; missing values become ``False``.
        threshold: Non-negative, finite threshold.

    Returns:
        Boolean array with the same length as ``levels``.

    Raises:
        ValueError: ``threshold`` 为负、NaN 或无穷大，或 ``levels`` 不是一维数值序列。
    """
    limit = _validated_threshold(threshold)
    values = _as_float_array(levels, name="levels")
    if values.ndim != 1:
        raise ValueError(f"levels must be one-dimensional, got {values.ndim} dimensions")
    return (values >= limit) & np.isfinite(values)


# --------------------------------------------------------------------------- #
# 事件段与事件级指标
# --------------------------------------------------------------------------- #


def event_segments(
    mask: Sequence[bool],
    timestamps: Sequence[pd.Timestamp] | pd.DatetimeIndex | None = None,
) -> list[EventSegment]:
    """Merge consecutive ``True`` steps into event segments.

    Args:
        mask: Boolean mask ordered by time.
        timestamps: Optional timestamps aligned with ``mask``; when given, each
            segment carries its start and end time.

    Raises:
        ValueError: ``mask`` 不是布尔序列，或 ``timestamps`` 长度与 ``mask`` 不一致。
    """
    flags = _as_bool_array(mask, name="mask")
    index = _as_datetime_index(timestamps, length=len(flags), name="timestamps")

    segments: list[EventSegment] = []
    cursor = 0
    while cursor < flags.size:
        if not flags[cursor]:
            cursor += 1
            continue
        end = cursor
        while end + 1 < flags.size and flags[end + 1]:
            end += 1
        segments.append(
            EventSegment(
                start_index=int(cursor),
                end_index=int(end),
                start_time=None if index is None else index[cursor],
                end_time=None if index is None else index[end],
            )
        )
        cursor = end + 1
    return segments


def evaluate_event_metrics(
    observed_mask: Sequence[bool],
    predicted_mask: Sequence[bool],
) -> EventMetrics:
    """Point-level precision / recall / F1 for one (segment, horizon, model) cell.

    事件点为"落在支持集内的目标时刻是否被标注为研究性高水位"。两种"指标无定义"的格子都
    **一律返回 ``None``，绝不用 ``0`` 代替**：

    - 观测事件点为 ``0`` → 三项为 ``None``，``reason`` 为 ``no_observed_events``；
    - 观测有事件、但没有任何预测事件 → 三项为 ``None``，``reason`` 为 ``no_predicted_events``。

    两种情况下 ``tp`` / ``fp`` / ``fn`` 与事件点数仍然给出，读者可以据此判断"全部漏报"
    或"没有评分对象"，而不是把 ``0`` 误读成"精确率等于零"。

    Raises:
        ValueError: 两个掩码长度不同，或存在非布尔输入。
    """
    observed = _as_bool_array(observed_mask, name="observed_mask")
    predicted = _as_bool_array(predicted_mask, name="predicted_mask")
    if observed.size != predicted.size:
        raise ValueError(
            "observed_mask and predicted_mask must have the same length, got "
            f"{observed.size} and {predicted.size}"
        )

    true_positive = int(np.count_nonzero(observed & predicted))
    false_positive = int(np.count_nonzero(~observed & predicted))
    false_negative = int(np.count_nonzero(observed & ~predicted))
    observed_points = int(np.count_nonzero(observed))
    predicted_points = int(np.count_nonzero(predicted))

    if observed_points == 0:
        precision: float | None = None
        recall: float | None = None
        f1: float | None = None
        reason: str | None = NO_OBSERVED_EVENTS
    elif predicted_points == 0:
        # precision 的分母为 0，recall 虽可定义为 0，但 F1 随之失去定义；为了让三种指标
        # 的语义保持一致，这里同样返回 None，并用 fn 表达"全部漏报"这一事实。
        precision = None
        recall = None
        f1 = None
        reason = NO_PREDICTED_EVENTS
    else:
        precision = true_positive / predicted_points
        recall = true_positive / observed_points
        denominator = float(precision + recall)
        f1 = 0.0 if denominator == 0.0 else 2.0 * precision * recall / denominator
        reason = None

    return EventMetrics(
        precision=precision,
        recall=recall,
        f1=f1,
        tp=true_positive,
        fp=false_positive,
        fn=false_negative,
        observed_event_points=observed_points,
        observed_event_segments=len(event_segments(observed)),
        predicted_event_points=predicted_points,
        predicted_event_segments=len(event_segments(predicted)),
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# 峰值误差与滞后
# --------------------------------------------------------------------------- #


def peak_underestimation(
    observed: Sequence[float] | pd.Series,
    prediction: Sequence[float] | pd.Series,
    times: Sequence[pd.Timestamp] | pd.DatetimeIndex | None = None,
) -> PeakError:
    """Measure the error at the observed peak and the largest absolute error.

    Args:
        observed: Observed target series.
        prediction: Predicted series aligned with ``observed``.
        times: Timestamps of each row; inferred from the pandas index when omitted.

    Returns:
        A :class:`PeakError`. All fields are ``None`` when no pairwise-valid row exists.
    """
    levels, forecasts, index = _aligned_series(observed, prediction, times)
    valid = np.isfinite(levels) & np.isfinite(forecasts)
    if not bool(valid.any()):
        return PeakError(None, None, None, None, None, None, None)

    masked_levels = np.where(valid, levels, -np.inf)
    peak_position = int(np.argmax(masked_levels))
    errors = np.where(valid, np.abs(forecasts - levels), -np.inf)
    worst_position = int(np.argmax(errors))

    return PeakError(
        observed_peak=float(levels[peak_position]),
        observed_peak_time=_stamp(index, peak_position),
        prediction_at_peak=float(forecasts[peak_position]),
        underestimation=float(levels[peak_position] - forecasts[peak_position]),
        max_absolute_error=float(errors[worst_position]),
        max_absolute_error_time=_stamp(index, worst_position),
        max_absolute_error_direction=_direction(
            float(forecasts[worst_position] - levels[worst_position])
        ),
    )


def peak_lag_hours(
    observed: Sequence[float] | pd.Series,
    prediction: Sequence[float] | pd.Series,
    times: Sequence[pd.Timestamp] | pd.DatetimeIndex | None = None,
) -> int | None:
    """Signed peak lag in hours: prediction peak time minus observed peak time.

    正值 = 预测峰值比观测峰值晚到（滞后）；负值 = 预测峰值提前。任一序列完全没有有效
    观测时返回 ``None``（滞后无定义），不得用 ``0`` 代替。
    """
    levels, forecasts, index = _aligned_series(observed, prediction, times)
    if index is None:
        raise ValueError("times are required to express the peak lag in hours")

    valid_levels = np.isfinite(levels)
    valid_forecasts = np.isfinite(forecasts)
    if not bool(valid_levels.any()) or not bool(valid_forecasts.any()):
        return None

    observed_position = int(np.argmax(np.where(valid_levels, levels, -np.inf)))
    prediction_position = int(np.argmax(np.where(valid_forecasts, forecasts, -np.inf)))
    delta = index[prediction_position] - index[observed_position]
    return int(round(delta.total_seconds() / 3600.0))


def earliest_warning_lead(
    event: EventSegment,
    *,
    issue_times: Sequence[pd.Timestamp] | pd.DatetimeIndex,
    target_times: Sequence[pd.Timestamp] | pd.DatetimeIndex,
    predictions: Sequence[float] | pd.Series,
    threshold: float,
) -> EarlyLead:
    """Advance time (in hours) at which this event was first foreseen.

    判定口径：只看**目标时刻落在该事件段窗口内**且预测值 ≥ 阈值的那批行，其中**发布时间
    最早**的那一行给出提前量 ``事件开始时刻 − 发布时间``。最长时域只有 6 小时，提前量
    天然不超过 6 小时；发布时间不早于事件开始时刻时返回 ``None``（没有提前量）而不是 0。

    Raises:
        ValueError: 事件缺少起止时刻，或三个序列长度不一致。
    """
    if event.start_time is None or event.end_time is None:
        raise ValueError("event must carry start_time and end_time")

    limit = _validated_threshold(threshold)
    issued = _as_datetime_index(issue_times, length=None, name="issue_times")
    targets = _as_datetime_index(target_times, length=None, name="target_times")
    values = _as_float_array(predictions, name="predictions")
    if not (issued.size == targets.size == values.size):
        raise ValueError(
            "issue_times, target_times and predictions must have the same length, got "
            f"{issued.size}, {targets.size} and {values.size}"
        )

    inside = (targets >= event.start_time) & (targets <= event.end_time)
    crossing = inside & np.isfinite(values) & (values >= limit)
    if not bool(crossing.any()):
        return EarlyLead(None, None, None, None, NO_CROSSING_REASON)

    positions = list(np.flatnonzero(crossing))
    earliest_position = min(positions, key=lambda position: issued[position])
    stamp = issued[earliest_position]
    lead = (event.start_time - stamp).total_seconds() / 3600.0
    if lead <= 0:
        return EarlyLead(
            None,
            stamp,
            targets[earliest_position],
            float(values[earliest_position]),
            NO_ADVANCE_REASON,
        )
    return EarlyLead(
        int(round(lead)),
        stamp,
        targets[earliest_position],
        float(values[earliest_position]),
        None,
    )


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _validated_threshold(threshold: object) -> float:
    if isinstance(threshold, bool) or not isinstance(threshold, Real):
        raise ValueError(f"threshold must be a finite non-negative number, got {threshold!r}")
    value = float(threshold)
    if not np.isfinite(value) or value < 0.0:
        raise ValueError(f"threshold must be a finite non-negative number, got {threshold!r}")
    return value


def _validated_quantile(quantile: object) -> float:
    if isinstance(quantile, bool) or not isinstance(quantile, Real):
        raise ValueError(f"quantile must be a number in (0, 1], got {quantile!r}")
    value = float(quantile)
    if not np.isfinite(value) or not 0.0 < value <= 1.0:
        raise ValueError(f"quantile must be a number in (0, 1], got {quantile!r}")
    return value


def _as_bool_array(mask: object, *, name: str) -> np.ndarray:
    values = np.asarray(mask)
    if values.dtype != np.bool_:
        raise ValueError(f"{name} must be a boolean array, got dtype {values.dtype}")
    if values.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got {values.ndim} dimensions")
    return values


def _as_float_array(values: object, *, name: str) -> np.ndarray:
    if isinstance(values, pd.Series):
        return values.to_numpy(dtype="float64")
    try:
        return np.asarray(values, dtype="float64")
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a numeric sequence, got {values!r}") from error


def _as_datetime_index(
    timestamps: object,
    *,
    length: int | None,
    name: str,
) -> pd.DatetimeIndex | None:
    if timestamps is None:
        return None
    if not isinstance(timestamps, pd.DatetimeIndex):
        try:
            timestamps = pd.DatetimeIndex(list(timestamps))  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must hold timestamps, got {timestamps!r}") from error
    index = timestamps
    if length is not None and index.size != length:
        raise ValueError(f"{name} must have the same length as the mask, got {index.size}")
    return index


def _aligned_series(
    observed: object,
    prediction: object,
    times: object,
) -> tuple[np.ndarray, np.ndarray, pd.DatetimeIndex | None]:
    """Coerce one observed / prediction pair plus their timestamps into aligned arrays."""
    levels = _as_float_array(observed, name="observed")
    forecasts = _as_float_array(prediction, name="prediction")
    if levels.size != forecasts.size:
        raise ValueError(
            "observed and prediction must have the same length, got "
            f"{levels.size} and {forecasts.size}"
        )

    stamps: object = times
    if stamps is None:
        for candidate in (observed, prediction):
            if isinstance(candidate, pd.Series) and isinstance(candidate.index, pd.DatetimeIndex):
                stamps = candidate.index
                break
    index = _as_datetime_index(stamps, length=levels.size, name="times")
    return levels, forecasts, index


def _stamp(index: pd.DatetimeIndex | None, position: int) -> pd.Timestamp | None:
    return None if index is None else index[position]


def _direction(residual: float) -> str:
    if residual < 0.0:
        return DIRECTION_UNDER
    if residual > 0.0:
        return DIRECTION_OVER
    return DIRECTION_NONE

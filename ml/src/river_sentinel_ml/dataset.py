"""Build causal water-level sequences and chronologically ordered splits.

该模块位于规范化之后、特征构造之前：

1. :func:`resample_water_level` 把逐条观测聚合成等间隔序列。每个频率桶只取
   **桶内最后一次观测**，不使用桶外或未来数据；没有任何观测的桶补齐为一行，
   使缺失显式可见；只有连续缺失长度不超过阈值的短缺口做线性插值，并且每一行
   是否被插补都不隐藏（``is_imputed`` 与 ``quality_flag == "imputed"``）。
2. :func:`chronological_split` 按时间先后切分训练、验证与测试段。它拒绝乱序或
   重复的时间戳，而不是静默排序或去重：静默修正既会掩盖上游缺陷，也可能把未来
   信息带进历史段。

两个函数都是纯函数：不读文件、不访问网络、不写盘、不修改入参、无随机性，
同一输入的两次调用结果完全一致。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Integral, Real

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype
from pandas.tseries.frequencies import to_offset

OUTPUT_COLUMNS: tuple[str, ...] = (
    "source_record_id",
    "observed_at",
    "water_level_m",
    "station_code",
    "station_name",
    "quality_flag",
    "is_imputed",
)

#: Source columns carried through unchanged when present in the input frame.
SOURCE_COLUMNS: tuple[str, ...] = (
    "source_record_id",
    "station_code",
    "station_name",
    "quality_flag",
)

IMPUTED_QUALITY_FLAG = "imputed"

#: Three non-empty partitions plus a meaningful learnable history need this many rows.
MIN_SPLIT_ROWS = 7

DEFAULT_FREQUENCY = "1h"
DEFAULT_STEPS = 2
DEFAULT_TRAIN_RATIO = 0.70
DEFAULT_VALIDATION_RATIO = 0.15

_GRID_KEY = "_bucket"


@dataclass(frozen=True)
class DatasetSplit:
    """Three chronologically ordered partitions of one sequence.

    Attributes:
        train: Earliest rows, used to fit parameters, scalers and models.
        validation: Middle rows, used for model and hyper-parameter selection.
        test: Latest rows, touched only for the final reported scores.
    """

    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame


def resample_water_level(
    frame: pd.DataFrame,
    frequency: str = DEFAULT_FREQUENCY,
    max_interpolation_steps: int = DEFAULT_STEPS,
) -> pd.DataFrame:
    """Aggregate observations into a causal, evenly spaced sequence.

    Args:
        frame: Normalized frame from :mod:`river_sentinel_ml.normalize`, holding at
            least ``observed_at`` (timezone-aware timestamps) and ``water_level_m``.
            Its rows do not need to be sorted. When a ``station_code`` column is
            present it must describe exactly one station: selecting a station is the
            caller's job, not this function's.
        frequency: Pandas frequency alias giving the bucket width, e.g. ``"1h"`` or
            ``"30min"``. The bucket is left-closed and the output timestamp is the
            bucket's left edge.
        max_interpolation_steps: Largest **consecutive missing step count** that is
            allowed to be linearly interpolated. ``0`` disables interpolation.

    Returns:
        A frame whose column set is exactly :data:`OUTPUT_COLUMNS`, one row per grid
        step between the first and last bucket, sorted by ``observed_at`` with a
        default integer index. Rows without any observation have missing
        ``water_level_m`` and empty source fields; rows filled from their two nearest
        valid neighbours have ``is_imputed=True`` and ``quality_flag="imputed"``.

        Bucket semantics: a grid step carries the **last observation inside its own
        bucket**, while its ``observed_at`` is that bucket's left edge. A row labelled
        ``t`` can therefore hold a reading recorded up to one bucket width after ``t``
        -- one hour at ``"1h"``. This bounded look-ahead is deliberate: it keeps each
        value on the wall-clock hour it describes. Downstream baselines, reports and
        the paper must state it, because it makes every horizon shorter than one
        bucket width look better than a strictly causal series would.

    Raises:
        TypeError: ``frame`` is not a :class:`pandas.DataFrame`.
        ValueError: A required column is missing, ``observed_at`` is not timezone-aware
            datetime data, ``observed_at`` contains missing timestamps,
            ``water_level_m`` is not numeric, ``frequency`` is not a valid frequency
            alias, ``max_interpolation_steps`` is not a non-negative integer, or the
            frame mixes several ``station_code`` values.
    """
    _require_frame(frame, name="frame")
    for column in ("observed_at", "water_level_m"):
        if column not in frame.columns:
            raise ValueError(f"frame must contain a '{column}' column")

    stamps = frame["observed_at"]
    if not isinstance(stamps.dtype, pd.DatetimeTZDtype):
        raise ValueError(
            "frame['observed_at'] must be timezone-aware datetime data, "
            f"got {stamps.dtype!r}; naive timestamps make the bucket grid ambiguous"
        )
    if bool(stamps.isna().any()):
        raise ValueError("frame['observed_at'] must not contain missing timestamps")
    if not is_numeric_dtype(frame["water_level_m"]):
        raise ValueError(
            f"frame['water_level_m'] must be numeric, got {frame['water_level_m'].dtype!r}"
        )

    offset = _validated_frequency(frequency)
    steps = _validated_steps(max_interpolation_steps)
    _validated_single_station(frame)

    if frame.empty:
        return _empty_resampled(stamps.dtype)

    work = frame.copy()
    buckets = stamps.dt.floor(offset)
    work[_GRID_KEY] = buckets
    # Stable ordering lets ``keep="last"`` pick the latest observation per bucket while
    # preserving the upstream ordering of records sharing one timestamp. The bucket's
    # left edge is the row's label, so the value can postdate it by up to one bucket
    # width -- see the bounded look-ahead note in the docstring.
    work = work.sort_values([_GRID_KEY, "observed_at"], kind="stable")
    aggregated = work.drop_duplicates(subset=_GRID_KEY, keep="last").set_index(_GRID_KEY)

    grid = pd.date_range(start=buckets.min(), end=buckets.max(), freq=offset, tz=stamps.dtype.tz)
    gridded = aggregated.reindex(grid)

    levels = gridded["water_level_m"].to_numpy(dtype="float64")
    imputed = np.zeros(grid.size, dtype=bool)
    if steps > 0:
        levels, imputed = _interpolate_short_gaps(levels, steps)

    # Source columns pass through untouched on observed rows; only the quality flag
    # is rewritten for imputed rows so the fill stays visible downstream.
    source = {column: _carry_source_column(gridded, column, grid) for column in SOURCE_COLUMNS}
    flags = source["quality_flag"].to_numpy(dtype=object, copy=True)
    flags[imputed] = IMPUTED_QUALITY_FLAG

    return pd.DataFrame(
        {
            "source_record_id": source["source_record_id"].to_numpy(dtype=object),
            "observed_at": gridded.index.to_series().astype(stamps.dtype).reset_index(drop=True),
            "water_level_m": pd.Series(levels, dtype="float64"),
            "station_code": source["station_code"].to_numpy(dtype=object),
            "station_name": source["station_name"].to_numpy(dtype=object),
            "quality_flag": pd.Series(flags, dtype="object"),
            "is_imputed": pd.Series(imputed, dtype=bool),
        }
    ).reset_index(drop=True)


def chronological_split(
    frame: pd.DataFrame,
    train_ratio: float = DEFAULT_TRAIN_RATIO,
    validation_ratio: float = DEFAULT_VALIDATION_RATIO,
) -> DatasetSplit:
    """Split one sequence into train, validation and test in chronological order.

    Args:
        frame: Time-ordered frame carrying an ``observed_at`` column whose values are
            unique and strictly increasing. Rows keep their input column set.
        train_ratio: Share of rows assigned to ``train`` (rounded down).
        validation_ratio: Share of rows assigned to ``validation`` (rounded down);
            every remaining row belongs to ``test``.

    Returns:
        A :class:`DatasetSplit` whose parts are non-empty, disjoint, ordered as
        ``train < validation < test`` and reset to a default integer index. Together
        they cover every input row exactly once.

    Raises:
        TypeError: ``frame`` is not a :class:`pandas.DataFrame`.
        ValueError: ``observed_at`` is missing, contains missing or duplicate values,
            is not strictly increasing, an input ratio is outside ``(0, 1)``, the two
            ratios add up to 1 or more, fewer than :data:`MIN_SPLIT_ROWS` rows remain,
            or rounding leaves an empty partition.
    """
    _require_frame(frame, name="frame")
    if "observed_at" not in frame.columns:
        raise ValueError("frame must contain an 'observed_at' column")
    stamps = frame["observed_at"]
    if not is_datetime64_any_dtype(stamps.dtype):
        raise ValueError(f"frame['observed_at'] must be datetime data, got {stamps.dtype!r}")
    if bool(stamps.isna().any()):
        raise ValueError("frame['observed_at'] must not contain missing timestamps")
    if not bool(stamps.is_unique):
        raise ValueError("frame['observed_at'] must not contain duplicate timestamps")
    if not bool(stamps.is_monotonic_increasing):
        raise ValueError("frame['observed_at'] must be monotonically increasing in time")

    share_train = _validated_ratio(train_ratio, "train_ratio")
    share_validation = _validated_ratio(validation_ratio, "validation_ratio")
    if share_train + share_validation >= 1.0:
        raise ValueError(
            "train_ratio + validation_ratio must be smaller than 1 "
            f"(got {share_train!r} + {share_validation!r} = "
            f"{share_train + share_validation!r}); otherwise test would have no row "
            "and the split must not return an empty partition"
        )

    total = len(frame)
    if total < MIN_SPLIT_ROWS:
        raise ValueError(f"chronological split needs at least {MIN_SPLIT_ROWS} rows, got {total}")

    count_train = int(total * share_train)
    count_validation = int(total * share_validation)
    count_test = total - count_train - count_validation
    sizes = {"train": count_train, "validation": count_validation, "test": count_test}
    # ``<= 0`` and not ``== 0``: rounding can also drive a partition negative, and a
    # negative slicing bound would silently yield an empty frame downstream.
    empty = [name for name, size in sizes.items() if size <= 0]
    if empty:
        raise ValueError(
            f"ratios {share_train!r} and {share_validation!r} leave an empty partition "
            f"({', '.join(empty)}) for {total} rows; increase it or add more rows"
        )

    start_validation = count_train
    start_test = count_train + count_validation
    return DatasetSplit(
        train=frame.iloc[:start_validation].copy().reset_index(drop=True),
        validation=frame.iloc[start_validation:start_test].copy().reset_index(drop=True),
        test=frame.iloc[start_test:].copy().reset_index(drop=True),
    )


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _require_frame(frame: object, *, name: str) -> pd.DataFrame:
    """Reject anything that is not a DataFrame, returning the frame itself."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{name} must be a pandas DataFrame, got {type(frame).__name__}")
    return frame


def _validated_single_station(frame: pd.DataFrame) -> None:
    """Reject a frame mixing several stations; missing codes are not a station.

    Folding observations from two stations into one grid would interleave unrelated
    rivers into a single series, which no downstream step can detect afterwards.
    Selecting one station belongs to the caller's pipeline, so the only safe answer
    here is to fail loudly.
    """
    if "station_code" not in frame.columns:
        return
    stations = frame["station_code"].dropna().unique()
    if stations.size > 1:
        listed = ", ".join(str(code) for code in stations[:3])
        suffix = ", ..." if stations.size > 3 else ""
        raise ValueError(
            "frame must describe a single station: 'station_code' has "
            f"{stations.size} distinct values ({listed}{suffix}); "
            "filter to one station before resampling"
        )


def _validated_frequency(frequency: object) -> pd.DateOffset:
    """Parse ``frequency`` into a pandas offset, or raise an explicit ``ValueError``."""
    if not isinstance(frequency, str) or not frequency.strip():
        raise ValueError(
            "frequency must be a non-empty pandas frequency alias such as "
            f"'{DEFAULT_FREQUENCY}' or '30min', got {frequency!r}"
        )
    try:
        offset = to_offset(frequency)
    except (ValueError, TypeError) as error:
        raise ValueError(
            "frequency must be a valid pandas frequency alias such as "
            f"'{DEFAULT_FREQUENCY}' or '30min', got {frequency!r}"
        ) from error
    if offset is None:
        raise ValueError(f"frequency {frequency!r} does not describe a fixed step size")
    return offset


def _validated_steps(steps: object) -> int:
    """Validate ``max_interpolation_steps`` as a non-negative integer."""
    if isinstance(steps, bool) or not isinstance(steps, Integral):
        raise ValueError(f"max_interpolation_steps must be a non-negative integer, got {steps!r}")
    value = int(steps)
    if value < 0:
        raise ValueError(f"max_interpolation_steps must be non-negative, got {steps!r}")
    return value


def _validated_ratio(value: object, name: str) -> float:
    """Validate one split ratio as a finite number strictly between 0 and 1."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a number strictly between 0 and 1, got {value!r}")
    number = float(value)
    if not math.isfinite(number) or not 0.0 < number < 1.0:
        raise ValueError(f"{name} must be strictly between 0 and 1, got {value!r}")
    return number


# --------------------------------------------------------------------------- #
# Resampling internals
# --------------------------------------------------------------------------- #


def _carry_source_column(
    gridded: pd.DataFrame,
    column: str,
    grid: pd.DatetimeIndex,
) -> pd.Series:
    """Return one source column over the full grid, missing rows as ``None``."""
    if column not in gridded.columns:
        return pd.Series([None] * grid.size, index=grid, dtype="object")
    values = gridded[column]
    return pd.Series(values.where(values.notna(), None), index=grid, dtype="object")


def _interpolate_short_gaps(
    levels: np.ndarray,
    steps: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Linearly fill NaN runs no longer than ``steps``, reporting what was filled.

    Args:
        levels: Level values aligned to an evenly spaced grid; may contain NaN.
        steps: Maximum run length that may be interpolated.

    Returns:
        A copy of ``levels`` where eligible inner runs are linearly interpolated
        between their two nearest valid neighbours, and a boolean mask marking the
        filled positions. Leading or trailing runs are never filled: doing so would
        need a value from further in the future or further back than the grid itself.
    """
    filled = levels.astype("float64", copy=True)
    imputed = np.zeros(filled.size, dtype=bool)
    missing = np.isnan(filled)
    size = filled.size
    cursor = 0

    while cursor < size:
        if not missing[cursor]:
            cursor += 1
            continue
        start = cursor
        while cursor < size and missing[cursor]:
            cursor += 1
        end = cursor  # exclusive: the first valid position after the run

        left = start - 1
        right = end
        if left >= 0 and right < size and (end - start) <= steps:
            anchors = np.array([left, right], dtype="float64")
            filled[start:end] = np.interp(
                np.arange(start, end, dtype="float64"),
                anchors,
                filled[[left, right]],
            )
            imputed[start:end] = True

    return filled, imputed


def _empty_resampled(stamp_dtype: pd.DatetimeTZDtype) -> pd.DataFrame:
    """Return the typed empty frame for an empty but valid input."""
    return pd.DataFrame(
        {
            "source_record_id": pd.Series([], dtype="object"),
            "observed_at": pd.Series([], dtype=stamp_dtype),
            "water_level_m": pd.Series([], dtype="float64"),
            "station_code": pd.Series([], dtype="object"),
            "station_name": pd.Series([], dtype="object"),
            "quality_flag": pd.Series([], dtype="object"),
            "is_imputed": pd.Series([], dtype=bool),
        }
    )

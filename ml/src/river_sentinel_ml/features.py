"""小时特征构造与水雨时间对齐（DS-4b）。

本模块位于 :mod:`river_sentinel_ml.dataset` 之后、基线（DS-5b）与模型（DS-6）之前，
把两份已经落盘的**小时**序列对齐成一张**严格因果**的特征矩阵：

1. :func:`load_hourly_rainfall_tables` 递归读取 Open-Meteo 小时降雨 CSV，按
   ``observed_at`` + ``point_id`` 归并，并对多个网格点做**等权区域平均**，输出
   每个小时恰好一行的 ``observed_at / rainfall_mm / point_count``。缺失小时补
   ``NaN`` 并如实计数，绝不用插值掩盖。
2. :func:`build_hourly_features` 先把完整小时网格按时间顺序切成
   ``train / validation / test`` 三段，再在**每一段内部**独立计算水位滞后、
   降雨滞后、降雨累计窗口与多时域标签。任何特征只依赖 ``<= t`` 的信息，任何
   窗口与标签都不跨段，段首一律为 ``NaN``。
3. :func:`feature_columns` 给出交给模型的特征列清单，把键、段标签、标签列与
   溯源列排除在外。

三条不可让步的边界：

- **严格因果**：不得出现未来水位、未来降雨或全数据集统计量；
- **不跨段**：滞后、窗口与标签都不得借用相邻段的末尾值，否则训练集就会看见
  验证/测试期的信息；
- **插补可追溯**：插补值保留在 ``water_level_t`` 与滞后列中，但目标时刻被插补
  的样本一律标记为不可用（C-5），不得参与训练与评估。

口径声明（论文与页面必须同样表述）：本模块使用的降雨是 Open-Meteo **Best Match
网格化再分析**降雨，5 个网格点等权平均只是「文成县区域降雨代理变量」；它属于历史
再分析网格数据，不是水文测站的仪器观测值，也不包含任何面向未来的气象信息。

三个函数都是纯函数：不读文件（读取函数只读传入目录）、不访问网络、不写盘、
不修改入参、无随机性；同一输入两次调用结果完全一致。
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Sequence
from numbers import Integral, Real
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype

#: 所有时间戳统一到项目时区，保证与 DS-2b / DS-3 的落盘格式一致。
TIMEZONE = "Asia/Shanghai"

#: :func:`load_hourly_rainfall_tables` 的输出列，顺序固定。
RAINFALL_COLUMNS: tuple[str, ...] = ("observed_at", "rainfall_mm", "point_count")

DEFAULT_RAINFALL_VALUE_COLUMN = "rain_mm"

DEFAULT_HORIZONS: tuple[int, ...] = (1, 3, 6)
DEFAULT_WATER_LAGS: tuple[int, ...] = (1, 2, 3, 6, 12, 24)
#: 降雨滞后包含 ``0``：``rain_lag_0`` 就是 ``t`` 时刻本身的降雨。
DEFAULT_RAIN_LAGS: tuple[int, ...] = (0, 1, 2, 3, 6, 12)
DEFAULT_RAIN_WINDOWS: tuple[int, ...] = (1, 3, 6, 12, 24)
DEFAULT_SPLIT_RATIOS: tuple[float, float, float] = (0.70, 0.15, 0.15)

WATER_REQUIRED_COLUMNS: tuple[str, ...] = (
    "observed_at",
    "water_level_m",
    "is_imputed",
    "source_record_id",
)
RAINFALL_REQUIRED_COLUMNS: tuple[str, ...] = RAINFALL_COLUMNS

#: 行本地列：这些列只描述 ``t`` 这一行，与所在段无关。
_ROW_LOCAL_EXCLUDED: frozenset[str] = frozenset(
    {"observed_at", "segment", "is_imputed_t", "source_record_id_t", "rain_point_count"}
)

#: 段内可计算列的前缀：标签与可用性标志不是模型输入。
_EXCLUDED_PREFIXES: tuple[str, ...] = ("target_h", "usable_h")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def load_hourly_rainfall_tables(
    directory: str | Path,
    *,
    points: Sequence[str] | None = None,
    value_column: str = DEFAULT_RAINFALL_VALUE_COLUMN,
) -> pd.DataFrame:
    """把一个小雨 CSV 目录读成一个等权区域平均的小时降雨序列。

    Args:
        directory: 降雨 CSV 的根目录；目录下的所有 ``*.csv`` 会被递归读取，
            子目录层级与文件命名不做假设（DS-3 的实际结构是
            ``<point_id>/<start>_<end>.csv``）。
        points: 参与平均的 ``point_id`` 子集；``None`` 表示使用目录中出现的全部点。
            指定了目录中不存在的点时抛 ``ValueError``，避免把拼写错误当成空数据。
        value_column: 作为降雨量主字段的列名，默认 ``rain_mm``。DS-3 同时导出了
            ``precipitation_mm``；把两种取值分别调用本函数即可做交叉核对。

    Returns:
        列顺序恒为 :data:`RAINFALL_COLUMNS` 的帧，每个小时恰好一行：``observed_at``
        为 ``datetime64[ns, Asia/Shanghai]``；``rainfall_mm`` 为该小时所有**可用**
        观测点的等权算术平均（全部缺失时为 ``NaN``）；``point_count`` 为该小时
        实际参与平均的点数（网格空缺小时为 ``0``）。时间范围覆盖 CSV 中出现过的
        最小与最大小时，中间不存在行的小时补 ``NaN`` 而不是插值。

    Raises:
        FileNotFoundError: ``directory`` 不是已存在的目录。
        ValueError: 目录下没有 CSV；某个文件缺少必需列（消息含缺失列名与文件路径）；
            ``value_column`` 不是非空字符串；时间戳为 naive 或携带多个 UTC 偏移；
            同一点出现重复的 ``(observed_at, point_id)``；``points`` 为空或包含未知点。
    """
    if not isinstance(value_column, str) or not value_column.strip():
        raise ValueError(f"value_column must be a non-empty column name, got {value_column!r}")
    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"rainfall directory does not exist: {directory}")
    files = sorted(path for path in root.rglob("*.csv") if path.is_file())
    if not files:
        raise ValueError(f"no CSV file found below rainfall directory {directory}")

    required = ("observed_at", "point_id", value_column)
    parts: list[pd.DataFrame] = []
    for path in files:
        frame = pd.read_csv(path)
        missing = [name for name in required if name not in frame.columns]
        if missing:
            raise ValueError(
                f"{path}: missing required column(s) {', '.join(missing)}; "
                f"expected at least observed_at, point_id and '{value_column}'"
            )
        parts.append(
            pd.DataFrame(
                {
                    "observed_at": _aware_timestamps(
                        frame["observed_at"], name=f"{path}: 'observed_at'"
                    ),
                    "point_id": frame["point_id"].astype("string").str.strip(),
                    "value": _numeric_values(frame[value_column], name=f"{path}: '{value_column}'"),
                }
            )
        )

    combined = pd.concat(parts, ignore_index=True)
    repeated = combined.duplicated(subset=["observed_at", "point_id"], keep=False)
    if bool(repeated.any()):
        example = combined.loc[repeated, "point_id"].iloc[0]
        raise ValueError(
            "rainfall tables contain duplicate (observed_at, point_id) observations "
            f"(for example point {example!r}); averaging them would double-count one point"
        )

    requested = _validated_points(points, combined["point_id"])
    if requested is not None:
        combined = combined.loc[combined["point_id"].isin(requested)]

    if combined.empty:
        return pd.DataFrame(
            {
                "observed_at": pd.Series([], dtype=f"datetime64[ns, {TIMEZONE}]"),
                "rainfall_mm": pd.Series([], dtype="float64"),
                "point_count": pd.array([], dtype="Int64"),
            }
        )

    grouped = combined.groupby("observed_at", sort=True)["value"]
    averages = grouped.mean()
    counts = grouped.count()
    grid = pd.date_range(averages.index.min(), averages.index.max(), freq="1h", tz=TIMEZONE)
    return pd.DataFrame(
        {
            "observed_at": grid,
            "rainfall_mm": averages.reindex(grid).to_numpy(dtype="float64"),
            "point_count": pd.array(
                counts.reindex(grid, fill_value=0).to_numpy(dtype="int64"), "Int64"
            ),
        },
        columns=list(RAINFALL_COLUMNS),
    )


def build_hourly_features(
    water: pd.DataFrame,
    rainfall: pd.DataFrame,
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    water_lags: Sequence[int] = DEFAULT_WATER_LAGS,
    rain_lags: Sequence[int] = DEFAULT_RAIN_LAGS,
    rain_windows: Sequence[int] = DEFAULT_RAIN_WINDOWS,
    split_ratios: Sequence[float] = DEFAULT_SPLIT_RATIOS,
) -> pd.DataFrame:
    """把小时水位与小时降雨对齐成严格因果的多时域特征矩阵。

    Args:
        water: 小时水位表。按**列名**识别，必需列为 ``observed_at``、
            ``water_level_m``、``is_imputed``、``source_record_id``；多出的列
            （例如 ``quality_flag``、``station_code``）被忽略，因此 DS-2b 落盘的
            5 列版本与 :func:`river_sentinel_ml.dataset.resample_water_level` 的
            7 列版本都可以直接传入。行序即为时间顺序，函数**不排序、不去重、
            不删行**。
        rainfall: :func:`load_hourly_rainfall_tables` 的输出（``observed_at`` /
            ``rainfall_mm`` / ``point_count``）。按 ``observed_at`` 精确左连接；
            水位某一小时在降雨表中缺失时，降雨列与 ``rain_point_count`` 为缺失。
        horizons: 预测步数（小时）。``target_h{h}`` 为 ``t + h`` 时刻的水位。
        water_lags: 水位滞后步数，必须为正整数；``water_level_lag_{k}`` 为 ``t - k``。
        rain_lags: 降雨滞后步数，允许 ``0``；``rain_lag_0`` 即 ``t`` 时刻降雨。
        rain_windows: 降雨累计窗口长度（小时），必须为正整数；
            ``rain_cum_{w}`` = ``t - w + 1`` 到 ``t``（含两端）的降雨之和，窗口
            未完整落入本段时为 ``NaN``。
        split_ratios: ``(train, validation, test)`` 三个比例，各自严格位于
            ``(0, 1)`` 且和为 1。切分口径与
            :func:`river_sentinel_ml.dataset.chronological_split` 完全一致：训练与
            验证段长度取 ``int(行数 * 比例)`` 向下取整，其余行归测试段。

    Returns:
        行数等于 ``water`` 行数、列顺序固定的特征矩阵，见
        ``docs/experiments/hourly-feature-contract.md``：

        - 键与段：``observed_at``、``segment``；
        - 当前值：``water_level_t``（插补值保留可见）；
        - 水位滞后：``water_level_lag_{k}``；
        - 降雨瞬时：``rain_lag_{k}``；降雨累计：``rain_cum_{w}``；
        - 时钟：``hour_of_day``、``month``（**不生成**汛期标志）；
        - 标签：``target_h{h}`` = ``t + h`` 时刻的水位，仅在本段内取值；
        - 可用性：``usable_h{h}`` 同时要求 ① 目标小时落在同一段内且非缺失，
          ② 本行 ``water_level_t`` 非缺失，③ 目标时刻未被插补（C-5）。三条缺一
          不可，否则 DS-5b 的持久性基线会拿到 ``NaN`` 锚点、评估会拿插补值当标签。
          这与 DS-2b 的「``t`` 与 ``t+h`` 均为有限值」样本口径一致，只额外扣掉
          目标时刻被插补的样本；
        - 溯源：``is_imputed_t``、``source_record_id_t``、``rain_point_count``。

    Raises:
        TypeError: ``water`` 或 ``rainfall`` 不是 :class:`pandas.DataFrame`。
        ValueError: 缺少必需列；水位时间戳不是时区感知、含缺失、重复或非递增；
            降雨时间戳重复；参数非法（``horizons`` 含 0 或负数、滞后步数非整数或
            小于下界、``rain_windows`` 含 0、``split_ratios`` 长度/取值/和不合法、
            切分后出现空段）。
    """
    _require_frame(water, name="water")
    _require_frame(rainfall, name="rainfall")
    horizon_steps = _validated_steps(horizons, name="horizons", minimum=1)
    water_steps = _validated_steps(water_lags, name="water_lags", minimum=1)
    rain_step_values = _validated_steps(rain_lags, name="rain_lags", minimum=0)
    rain_window_steps = _validated_steps(rain_windows, name="rain_windows", minimum=1)
    train_ratio, validation_ratio, _ = _validated_split_ratios(split_ratios)

    for column in WATER_REQUIRED_COLUMNS:
        if column not in water.columns:
            raise ValueError(f"water must contain a '{column}' column")
    for column in RAINFALL_REQUIRED_COLUMNS:
        if column not in rainfall.columns:
            raise ValueError(f"rainfall must contain a '{column}' column")

    stamps = _aware_timestamps(water["observed_at"], name="water['observed_at']")
    if not bool(stamps.is_unique):
        raise ValueError("water['observed_at'] must not contain duplicate timestamps")
    if not bool(stamps.is_monotonic_increasing):
        raise ValueError("water['observed_at'] must be monotonically increasing")
    levels = _numeric_values(water["water_level_m"], name="water['water_level_m']").to_numpy(
        dtype="float64"
    )
    imputed = _boolean_values(water["is_imputed"], name="water['is_imputed']")
    record_ids = water["source_record_id"].to_numpy(dtype=object)

    rain_aligned, count_aligned = _align_rainfall(rainfall, stamps)
    total = levels.size

    # 与 ``chronological_split`` 相同的整数边界：先切分，再在段内计算窗口。
    count_train = int(total * train_ratio)
    count_validation = int(total * validation_ratio)
    count_test = total - count_train - count_validation
    partitions = (
        ("train", 0, count_train),
        ("validation", count_train, count_train + count_validation),
        ("test", count_train + count_validation, total),
    )
    for label, start, stop in partitions:
        if total and stop - start <= 0:
            raise ValueError(
                f"split_ratios {tuple(split_ratios)!r} leave an empty '{label}' partition "
                f"for {total} rows; increase it or add more rows"
            )

    segment = np.array(
        ["train"] * count_train + ["validation"] * count_validation + ["test"] * count_test,
        dtype=object,
    )
    blocks: dict[str, np.ndarray] = {}
    for _label, start, stop in partitions:
        for name, values in _segment_block(
            levels,
            imputed,
            rain_aligned,
            start,
            stop,
            horizons=horizon_steps,
            water_lags=water_steps,
            rain_lags=rain_step_values,
            rain_windows=rain_window_steps,
        ).items():
            blocks[name] = np.concatenate([blocks[name], values]) if name in blocks else values

    ordered: dict[str, object] = {
        "observed_at": pd.DatetimeIndex(stamps),
        "segment": segment,
        "water_level_t": levels,
    }
    for step in water_steps:
        ordered[f"water_level_lag_{step}"] = blocks[f"water_level_lag_{step}"]
    for step in rain_step_values:
        ordered[f"rain_lag_{step}"] = blocks[f"rain_lag_{step}"]
    for window in rain_window_steps:
        ordered[f"rain_cum_{window}"] = blocks[f"rain_cum_{window}"]
    ordered["hour_of_day"] = stamps.dt.hour.to_numpy(dtype="int64")
    ordered["month"] = stamps.dt.month.to_numpy(dtype="int64")
    for horizon in horizon_steps:
        ordered[f"target_h{horizon}"] = blocks[f"target_h{horizon}"]
    for horizon in horizon_steps:
        ordered[f"usable_h{horizon}"] = blocks[f"usable_h{horizon}"]
    ordered["is_imputed_t"] = imputed.copy()
    ordered["source_record_id_t"] = record_ids
    ordered["rain_point_count"] = count_aligned
    return pd.DataFrame(ordered)


def feature_columns(frame: pd.DataFrame) -> tuple[str, ...]:
    """返回可以直接交给模型的列名，顺序与特征矩阵一致。

    排除项：键（``observed_at``）、段标签（``segment``）、标签与可用性
    （``target_h*`` / ``usable_h*``）以及溯源列（``is_imputed_t`` /
    ``source_record_id_t`` / ``rain_point_count``）。保留的是纯特征：
    ``water_level_t``、水位滞后、降雨滞后、降雨累计、``hour_of_day``、``month``。

    Args:
        frame: :func:`build_hourly_features` 的输出，或其他列集合兼容的帧。

    Returns:
        特征列名元组；输入没有列时返回空元组。

    Raises:
        TypeError: ``frame`` 不是 :class:`pandas.DataFrame`。
    """
    _require_frame(frame, name="frame")
    return tuple(
        str(name)
        for name in frame.columns
        if name not in _ROW_LOCAL_EXCLUDED and not str(name).startswith(_EXCLUDED_PREFIXES)
    )


# --------------------------------------------------------------------------- #
# Segment-local construction
# --------------------------------------------------------------------------- #


def _segment_block(
    levels: np.ndarray,
    imputed: np.ndarray,
    rain: np.ndarray,
    start: int,
    stop: int,
    *,
    horizons: tuple[int, ...],
    water_lags: tuple[int, ...],
    rain_lags: tuple[int, ...],
    rain_windows: tuple[int, ...],
) -> dict[str, np.ndarray]:
    """Compute every segment-dependent column for ``levels[start:stop]``.

    All shifting is done on the slice alone, which is what makes a segment head
    ``NaN`` instead of quietly reusing the previous segment's tail.
    """
    levels_segment = levels[start:stop]
    rain_segment = rain[start:stop]
    size = levels_segment.size

    block: dict[str, np.ndarray] = {}
    for step in water_lags:
        block[f"water_level_lag_{step}"] = _shifted(levels_segment, step)
    for step in rain_lags:
        block[f"rain_lag_{step}"] = _shifted(rain_segment, step)
    for window in rain_windows:
        block[f"rain_cum_{window}"] = _rolling_sum(rain_segment, window)
    for horizon in horizons:
        target = _shifted_forward(levels_segment, horizon)
        usable = np.zeros(size, dtype=bool)
        if horizon < size:
            head = slice(0, size - horizon)
            usable[head] = (
                ~np.isnan(target[head])  # the target hour must be observed
                & ~np.isnan(levels_segment[head])  # and so must the row's own hour
                & ~imputed[start + horizon : stop]  # C-5: never learn from an imputed target
            )
        block[f"target_h{horizon}"] = target
        block[f"usable_h{horizon}"] = usable
    return block


def _shifted(values: np.ndarray, steps: int) -> np.ndarray:
    """``values[t - steps]`` over one segment, padded with ``NaN`` at its head."""
    size = values.size
    shifted = np.full(size, np.nan, dtype="float64")
    if steps == 0:
        shifted[:] = values
    elif steps < size:
        shifted[steps:] = values[: size - steps]
    return shifted


def _shifted_forward(values: np.ndarray, steps: int) -> np.ndarray:
    """``values[t + steps]`` over one segment, padded with ``NaN`` at its tail."""
    size = values.size
    forward = np.full(size, np.nan, dtype="float64")
    if steps < size:
        forward[: size - steps] = values[steps:]
    return forward


def _rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
    """Inclusive ``window``-hour sum, ``NaN`` while the window is incomplete or holds a gap."""
    rolled = pd.Series(values, dtype="float64").rolling(window=window, min_periods=window).sum()
    return rolled.to_numpy(dtype="float64")


def _align_rainfall(
    rainfall: pd.DataFrame, stamps: pd.Series
) -> tuple[np.ndarray, pd.arrays.IntegerArray]:
    """Left-join the rainfall table onto the water grid by exact timestamp."""
    size = stamps.size
    if rainfall.empty:
        return np.full(size, np.nan, dtype="float64"), pd.array([pd.NA] * size, dtype="Int64")

    rain_stamps = _aware_timestamps(rainfall["observed_at"], name="rainfall['observed_at']")
    if not bool(rain_stamps.is_unique):
        raise ValueError("rainfall['observed_at'] must not contain duplicate timestamps")
    lookup = pd.DataFrame(
        {
            "rainfall_mm": _numeric_values(
                rainfall["rainfall_mm"], name="rainfall['rainfall_mm']"
            ).to_numpy(dtype="float64"),
            "point_count": pd.array(
                pd.to_numeric(rainfall["point_count"], errors="coerce"), dtype="Int64"
            ),
        },
        index=pd.DatetimeIndex(rain_stamps),
    )
    joined = lookup.reindex(pd.DatetimeIndex(stamps))
    return (
        joined["rainfall_mm"].to_numpy(dtype="float64"),
        pd.array(joined["point_count"], dtype="Int64"),
    )


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _require_frame(frame: object, *, name: str) -> pd.DataFrame:
    """Reject anything that is not a DataFrame, returning the frame itself."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{name} must be a pandas DataFrame, got {type(frame).__name__}")
    return frame


def _aware_timestamps(series: pd.Series, *, name: str) -> pd.Series:
    """Parse ``series`` into timezone-aware ``Asia/Shanghai`` timestamps.

    Naive timestamps and mixed UTC offsets are refused instead of guessed: either one
    would make the hourly grid -- and therefore every lag -- offset-dependent.
    """
    if is_datetime64_any_dtype(series.dtype):
        if not isinstance(series.dtype, pd.DatetimeTZDtype):
            raise ValueError(
                f"{name} must be timezone-aware; naive timestamps make the hourly grid ambiguous"
            )
        converted = series.dt.tz_convert(TIMEZONE)
    else:
        with warnings.catch_warnings():
            # 混合偏移在 pandas 2.x 只警告并退回 object dtype，这里把它升级为显式错误。
            warnings.simplefilter("ignore", FutureWarning)
            parsed = pd.to_datetime(series, format="ISO8601")
        if parsed.dtype == object:
            raise ValueError(
                f"{name} must carry one uniform UTC offset; naive timestamps or mixed "
                "offsets are not allowed"
            )
        if not isinstance(parsed.dtype, pd.DatetimeTZDtype):
            raise ValueError(
                f"{name} must be timezone-aware; naive timestamps make the hourly grid ambiguous"
            )
        offsets = {value.utcoffset() for value in parsed}
        if len(offsets) != 1:
            raise ValueError(f"{name} must carry one uniform UTC offset, found {len(offsets)}")
        converted = parsed.dt.tz_convert(TIMEZONE)

    if bool(converted.isna().any()):
        raise ValueError(f"{name} must not contain missing timestamps")
    return converted.reset_index(drop=True)


def _numeric_values(series: pd.Series, *, name: str) -> pd.Series:
    """Return ``series`` as ``float64``, or raise when it holds non-numeric text."""
    if not is_numeric_dtype(series):
        try:
            series = pd.to_numeric(series, errors="raise")
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be numeric, got {series.dtype!r}") from error
    return series.astype("float64")


def _boolean_values(series: pd.Series, *, name: str) -> np.ndarray:
    """Return ``series`` as a plain boolean array, accepting ``"True"``/``"False"`` text."""
    if series.dtype == bool:
        return series.to_numpy(dtype=bool)
    mapped = series.astype("string").str.strip().str.lower().map({"true": True, "false": False})
    if bool(mapped.isna().any()):
        raise ValueError(f"{name} must contain only boolean values, got {series.dtype!r}")
    return mapped.to_numpy(dtype=bool)


def _validated_steps(value: object, *, name: str, minimum: int) -> tuple[int, ...]:
    """Validate one window parameter as a non-empty tuple of unique integers."""
    items = _as_list(value, name=name, expected="a sequence of integers")
    if not items:
        raise ValueError(f"{name} must contain at least one step, got an empty sequence")
    steps: list[int] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, Integral):
            raise ValueError(f"{name} must contain integers >= {minimum}, got {item!r}")
        number = int(item)
        if number < minimum:
            raise ValueError(f"{name} must contain integers >= {minimum}, got {item!r}")
        steps.append(number)
    if len(set(steps)) != len(steps):
        raise ValueError(f"{name} must not contain duplicate steps, got {tuple(steps)!r}")
    return tuple(steps)


def _validated_split_ratios(value: object) -> tuple[float, float, float]:
    """Validate the three chronological split ratios, using ``chronological_split`` bounds."""
    items = _as_list(value, name="split_ratios", expected="three ratios")
    if len(items) != 3:
        raise ValueError(
            "split_ratios must contain exactly three ratios (train, validation, test), "
            f"got {len(items)}"
        )
    ratios: list[float] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, Real):
            raise ValueError(
                f"split_ratios must contain numbers strictly between 0 and 1, got {item!r}"
            )
        number = float(item)
        if not math.isfinite(number) or not 0.0 < number < 1.0:
            raise ValueError(f"split_ratios must be strictly between 0 and 1, got {item!r}")
        ratios.append(number)
    total = math.fsum(ratios)
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"split_ratios must sum to 1, got {total!r}")
    return (ratios[0], ratios[1], ratios[2])


def _as_list(value: object, *, name: str, expected: str) -> list[object]:
    """Coerce a non-string iterable into a list, raising ``ValueError`` naming the parameter."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        try:
            return list(value)  # type: ignore[arg-type]
        except TypeError as error:
            raise ValueError(f"{name} must be {expected}, got {value!r}") from error
    return list(value)


def _validated_points(points: Sequence[str] | None, available: pd.Series) -> list[str] | None:
    """Validate the requested ``point_id`` subset against the identifiers actually read."""
    if points is None:
        return None
    requested = _as_list(points, name="points", expected="a sequence of point identifiers")
    if not requested:
        raise ValueError("points must not be empty; pass None to average every point")
    names = [str(item).strip() for item in requested]
    known = set(available.tolist())
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ValueError(f"points not found in rainfall tables: {', '.join(unknown)}")
    return names

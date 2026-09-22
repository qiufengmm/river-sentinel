"""段内拟合/评估协议（DS-6）。

本模块只做一件事：把 DS-4b 冻结的**特征矩阵**按 ``segment`` 与 ``usable_h{h}``
切成"可以直接交给模型"的段内样本，并把"只由 train 拟合"的两条硬约束固化成可复用对象。

三条协议边界（HS-D-7 / HS-D-8 / C-5）：

1. **只用可用样本**：一行只有在 ``usable_h{h} = True`` 时才能作为 horizon ``h`` 的
   训练/评估样本；``usable_h{h}`` 已经同时保证了「目标小时在同段内且非缺失」
   「本行水位非缺失」「目标时刻未被插补」。
2. **不跨段**：滞后、累计窗口与标签都在 :mod:`river_sentinel_ml.features` 里段内构造，
   本模块只做切片，**不**重新排序、**不**重采样、**不**跨段补值。
3. **统计量只由 train 拟合**：:class:`Standardizer` 只接受训练段矩阵；它刻意**不**
   记录自己是用哪一段拟合的，避免调用方把 ``validation`` / ``test`` 传进来。

本模块不读写文件、不访问网络、无随机性、不修改入参。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Integral

import numpy as np
import pandas as pd

#: 段顺序固定：先训练、再验证、最后测试。任何报告与产物的键顺序都按此排列。
SEGMENT_ORDER: tuple[str, str, str] = ("train", "validation", "test")


# --------------------------------------------------------------------------- #
# Matrix / boolean helpers
# --------------------------------------------------------------------------- #


def as_float_matrix(features: object, *, columns: int | None = None) -> np.ndarray:
    """把 ``features`` 归一成 ``(n, columns)`` 的 ``float64`` 设计矩阵。

    Args:
        features: :class:`pandas.DataFrame` 或二维数组。
        columns: 期望的列数；给出时列数不符抛 ``ValueError``。

    Returns:
        二维 ``float64`` 数组；行数为 0 时形状为 ``(0, columns)``。输入中的 ``NaN``
        原样保留（缺失处理由各模型自己决定并计数）。

    Raises:
        ValueError: 输入不是二维矩阵，或列数与 ``columns`` 不符。
    """
    if isinstance(features, pd.DataFrame):
        matrix = features.to_numpy(dtype="float64")
    else:
        matrix = np.asarray(features, dtype="float64")
    if matrix.ndim != 2:
        raise ValueError(f"features must be a 2-D matrix, got shape {matrix.shape}")
    if columns is not None and matrix.shape[1] != columns:
        raise ValueError(f"features must have {columns} columns, got {matrix.shape[1]}")
    return matrix


def validated_horizon(value: object) -> int:
    """Return ``value`` as a positive ``int``, rejecting booleans and non-integers."""
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"horizon must be a positive integer, got {value!r}")
    number = int(value)
    if number <= 0:
        raise ValueError(f"horizon must be a positive integer, got {value!r}")
    return number


def boolean_values(series: pd.Series, *, name: str) -> np.ndarray:
    """Return one column as a plain boolean array, accepting ``"True"`` / ``"False"`` text.

    ``usable_h{h}`` 在内存里是 ``bool``，但 feature matrix 落盘再读回来时会变成
    ``True`` / ``False`` 文本，两种形态都必须接受，否则脚本读 CSV 就会静默拿到
    全 ``False``。
    """
    if series.dtype == bool:
        return series.to_numpy(dtype=bool)
    if isinstance(series.dtype, pd.BooleanDtype):
        if bool(series.isna().any()):
            raise ValueError(f"{name} must not contain missing values")
        return series.to_numpy(dtype=bool, na_value=False)
    mapped = (
        series.astype("string")
        .str.strip()
        .str.lower()
        .map({"true": True, "false": False, "1": True, "0": False, "1.0": True, "0.0": False})
    )
    if bool(mapped.isna().any()):
        raise ValueError(f"{name} must contain only boolean values, got {series.dtype!r}")
    return mapped.to_numpy(dtype=bool)


# --------------------------------------------------------------------------- #
# Flexible standardiser (fit on train only)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Standardizer:
    """Column-wise z-score standardiser whose statistics come from one segment only.

    只有 :meth:`fit` 的入参会被统计，因此调用方必须传入**训练段**矩阵。禁止用
    ``validation`` 或 ``test`` 拟合，也禁止用全量数据的均值/方差（HS-D-8）。
    """

    mean: np.ndarray
    scale: np.ndarray

    @classmethod
    def fit(cls, matrix: object) -> Standardizer:
        """Fit the statistics on a non-empty design matrix."""
        values = as_float_matrix(matrix)
        if values.shape[0] == 0:
            raise ValueError("cannot fit a Standardizer on zero rows")
        if bool(np.isnan(values).all(axis=0).any()):
            raise ValueError("cannot fit a Standardizer on a column that is entirely missing")
        mean = values.mean(axis=0)
        scale = values.std(axis=0)
        # 常数列的方差为 0：把 scale 置 1，避免除零并让该列在标准化后恒为 0。
        scale = np.where(scale > 0.0, scale, 1.0)
        return cls(mean=np.asarray(mean, dtype="float64"), scale=np.asarray(scale, dtype="float64"))

    def transform(self, matrix: object) -> np.ndarray:
        """Apply the fitted statistics; ``NaN`` inputs propagate to ``NaN`` outputs."""
        values = as_float_matrix(matrix, columns=self.mean.size)
        return (values - self.mean) / self.scale


# --------------------------------------------------------------------------- #
# Segment samples
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SegmentSamples:
    """One segment's usable rows for one horizon, already separated into X and y.

    ``features`` 保留 ``NaN``（段首的滞后/累计窗口天然缺失），是否剔除由模型决定：
    线性与 LSTM 走 :meth:`without_missing_features` 并上报剔除行数，树模型交给
    XGBoost 原生处理。
    """

    segment: str
    horizon: int
    feature_columns: tuple[str, ...]
    features: pd.DataFrame
    target: np.ndarray
    observed_at: pd.DatetimeIndex
    usable_rows: int
    missing_feature_rows: int

    @property
    def rows(self) -> int:
        """Number of usable rows handed to the model."""
        return int(len(self.features))

    def as_matrix(self) -> np.ndarray:
        """The design matrix, ``NaN`` included."""
        return as_float_matrix(self.features, columns=len(self.feature_columns))

    def without_missing_features(self) -> tuple[np.ndarray, np.ndarray, int]:
        """Drop rows carrying at least one missing feature.

        Returns:
            ``(matrix, target, dropped_rows)``；``dropped_rows`` 是实际剔除的行数，
            必须原样写进报告，不得静默丢弃。
        """
        matrix = self.as_matrix()
        if matrix.shape[0] == 0:
            return matrix, self.target, 0
        complete = ~np.isnan(matrix).any(axis=1)
        dropped = int(matrix.shape[0] - int(np.count_nonzero(complete)))
        return matrix[complete], self.target[complete], dropped


def build_segment_samples(
    frame: pd.DataFrame,
    *,
    horizon: int,
    feature_columns: Sequence[str],
    segment: str,
) -> SegmentSamples:
    """Extract one segment's usable samples for one horizon.

    Args:
        frame: DS-4b 特征矩阵（或列集合兼容的帧），必须含 ``segment``、
            ``observed_at``、``usable_h{h}``、``target_h{h}`` 与全部特征列。
        horizon: 预测步数，正整数。
        feature_columns: 本次实验使用的特征列（顺序即模型输入顺序）。
        segment: ``train`` / ``validation`` / ``test``。

    Returns:
        :class:`SegmentSamples`。该段所有行都不可用时返回**空样本**而不是抛错——
        零样本由 :func:`river_sentinel_ml.models.training.evaluate_predictions`
        按 ``sample_count=0`` + ``null`` + ``reason`` 兜底。

    Raises:
        TypeError: ``frame`` 不是 :class:`pandas.DataFrame`。
        ValueError: ``segment`` 不是三段之一；``frame`` 缺少必需列或某个特征列；
            ``horizon`` 非正整数；可用行的标签出现缺失（违反 ``usable_h{h}`` 契约）。
    """
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"frame must be a pandas DataFrame, got {type(frame).__name__}")
    steps = validated_horizon(horizon)
    if segment not in SEGMENT_ORDER:
        raise ValueError(f"segment must be one of {SEGMENT_ORDER}, got {segment!r}")
    columns = tuple(str(name) for name in feature_columns)
    if not columns:
        raise ValueError("feature_columns must not be empty")

    for required in ("segment", "observed_at", f"usable_h{steps}", f"target_h{steps}"):
        if required not in frame.columns:
            raise ValueError(f"frame must contain a '{required}' column")
    missing_features = [name for name in columns if name not in frame.columns]
    if missing_features:
        raise ValueError(f"frame is missing the feature column(s) {', '.join(missing_features)}")

    labels = frame["segment"].astype("string")
    present = sorted(set(labels.dropna().tolist()))
    if segment not in present:
        raise ValueError(
            f"frame does not contain the '{segment}' segment; frames it holds: {present}"
        )

    usable = boolean_values(frame[f"usable_h{steps}"], name=f"frame['usable_h{steps}']")
    selected = (labels == segment).to_numpy(dtype=bool) & usable
    block = frame.loc[selected]
    features = block.loc[:, list(columns)].astype("float64").reset_index(drop=True)
    target = block[f"target_h{steps}"].to_numpy(dtype="float64")
    if target.size and not bool(np.isfinite(target).all()):
        raise ValueError(
            f"frame['target_h{steps}'] must be finite wherever 'usable_h{steps}' is True"
        )
    return SegmentSamples(
        segment=segment,
        horizon=steps,
        feature_columns=columns,
        features=features,
        target=target,
        observed_at=pd.DatetimeIndex(block["observed_at"]),
        usable_rows=int(np.count_nonzero(selected)),
        missing_feature_rows=int(features.isna().any(axis=1).sum()),
    )


def build_all_segments(
    frame: pd.DataFrame,
    *,
    horizon: int,
    feature_columns: Sequence[str],
    segments: Sequence[str] = SEGMENT_ORDER,
) -> dict[str, SegmentSamples]:
    """Extract every requested segment, keyed and ordered by ``segments``."""
    return {
        name: build_segment_samples(
            frame, horizon=horizon, feature_columns=feature_columns, segment=name
        )
        for name in segments
    }

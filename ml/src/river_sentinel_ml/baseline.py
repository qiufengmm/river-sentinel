"""Causal persistence baseline for water-level forecasting."""

from numbers import Integral

import pandas as pd


def persistence_forecast(
    series: pd.Series,
    horizons: tuple[int, ...] = (1, 3, 6),
) -> pd.DataFrame:
    """Forecast each step horizon with the observation available at ``t - h``.

    每一行的时间戳 t 上，``prediction_h{h}`` 取该行之前第 h 步的观测值，
    因此预测只使用历史信息；``observed`` 始终为原序列在该时刻的观测值。
    单位口径为“步长”，不做时间对齐、重采样或间隔假设。
    """
    validated = _validate_horizons(horizons)
    _validate_index(series.index)

    frame = pd.DataFrame({"observed": series})
    for horizon in validated:
        frame[f"prediction_h{horizon}"] = series.shift(horizon)
    return frame


def _validate_horizons(horizons: tuple[int, ...]) -> tuple[int, ...]:
    validated: list[int] = []
    for horizon in horizons:
        if isinstance(horizon, bool) or not isinstance(horizon, Integral):
            raise ValueError(f"horizon must be a positive integer, got {horizon!r}")
        if horizon <= 0:
            raise ValueError(f"horizon must be greater than 0, got {horizon!r}")
        validated.append(int(horizon))
    return tuple(validated)


def _validate_index(index: pd.Index) -> None:
    if not index.is_unique:
        raise ValueError("series index must be unique")
    if not index.is_monotonic_increasing:
        raise ValueError("series index must be monotonically increasing")

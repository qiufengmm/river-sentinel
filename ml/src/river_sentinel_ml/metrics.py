"""Hydrological regression metrics for water-level forecasts."""

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ForecastMetrics:
    """Regression metrics for one forecast horizon."""

    sample_count: int
    mae: float
    rmse: float
    r2: float | None
    nse: float | None
    peak_absolute_error: float


def evaluate_forecasts(
    frame: pd.DataFrame,
    horizons: Iterable[int] = (1, 3, 6),
) -> dict[int, ForecastMetrics]:
    """Evaluate each horizon on the pairwise-valid rows of ``frame``.

    仅使用 ``observed`` 与该 horizon 预测值均非缺失的样本，成对缺失直接剔除；
    不存在的 horizon 列、零有效样本均抛 ``ValueError``；目标方差为零时
    ``r2`` 与 ``nse`` 为 ``None``。
    """
    if "observed" not in frame.columns:
        raise ValueError("frame must contain an 'observed' column")

    results: dict[int, ForecastMetrics] = {}
    for horizon in horizons:
        column = f"prediction_h{horizon}"
        if column not in frame.columns:
            raise ValueError(f"frame is missing the '{column}' column")
        results[int(horizon)] = _evaluate_pair(frame["observed"], frame[column])
    return results


def _evaluate_pair(observed: pd.Series, prediction: pd.Series) -> ForecastMetrics:
    target = np.asarray(observed.to_numpy(dtype="float64"))
    forecast = np.asarray(prediction.to_numpy(dtype="float64"))
    valid = ~np.isnan(target) & ~np.isnan(forecast)
    count = int(valid.sum())

    if count == 0:
        raise ValueError("no pairwise-valid samples available for this horizon")

    target = target[valid]
    forecast = forecast[valid]
    residual = forecast - target
    ss_res = float(np.sum(residual**2))
    ss_tot = float(np.sum((target - target.mean()) ** 2))
    skill = None if ss_tot == 0.0 else 1.0 - ss_res / ss_tot

    return ForecastMetrics(
        sample_count=count,
        mae=float(np.mean(np.abs(residual))),
        rmse=float(np.sqrt(ss_res / count)),
        r2=skill,
        nse=skill,
        peak_absolute_error=float(abs(target.max() - forecast.max())),
    )

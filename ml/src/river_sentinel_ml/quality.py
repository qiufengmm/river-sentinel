"""Sequence quality helpers shared by normalization and resampling.

The helpers here are pure functions: they never read files, never access the
network and never mutate their inputs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def infer_interval_minutes(index: pd.DatetimeIndex) -> float | None:
    """Infer the dominant sampling interval of a time index, in minutes.

    The result is the median of the strictly positive gaps between consecutive
    *unique* timestamps. Repeated timestamps therefore never contribute a zero
    gap, and an unsorted index never produces negative gaps.

    ``NaT`` entries are dropped before the computation. Values that are not a
    :class:`pandas.DatetimeIndex` are rejected with :class:`TypeError`.

    Returns ``None`` when fewer than two distinct timestamps remain, or when no
    positive gap exists, because the interval is then undefined.
    """
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError(f"index must be a pandas DatetimeIndex, got {type(index).__name__}")

    unique = index.dropna().unique().sort_values()
    if len(unique) < 2:
        return None

    gaps = (unique[1:] - unique[:-1]).total_seconds().to_numpy() / 60.0
    positive = gaps[gaps > 0]
    if positive.size == 0:
        return None
    return float(np.median(positive))

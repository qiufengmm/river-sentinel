"""Timezone, freshness and step-size helpers shared by services and repositories.

样例时间戳是**无时区**的本地时间，而所有对外契约都要求带时区；本模块是唯一的本地化
入口，避免各服务各自猜测时区。风险判定不读取时钟：时刻一律由调用方提供。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from .schemas.common import Freshness


def ensure_aware(value: datetime, timezone_name: str) -> datetime:
    """Attach ``timezone_name`` when ``value`` is naive, else return it unchanged."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=ZoneInfo(timezone_name))
    return value


def age_minutes(anchor_at: datetime, as_of: datetime) -> float:
    """Minutes between the latest usable observation and the request anchor."""
    return max(0.0, (as_of - anchor_at).total_seconds() / 60.0)


def freshness_of(age_in_minutes: float, stale_after_minutes: int) -> Freshness:
    """Classify freshness against the configured staleness budget."""
    if age_in_minutes > float(stale_after_minutes):
        return "stale"
    return "fresh"


def infer_step_minutes(index: pd.DatetimeIndex) -> float:
    """Infer the dominant sampling step in minutes from a sorted datetime index.

    小时网格与样例都是 1 小时步长；缺测造成的缝隙不应改变步长口径，因此取**中位数**
    而不是最小值。不足两个点时回退 60 分钟。
    """
    if len(index) < 2:
        return 60.0
    deltas = index.to_series().diff().dropna()
    if deltas.empty:
        return 60.0
    median = deltas.median()
    minutes = median.total_seconds() / 60.0
    if not minutes or minutes <= 0:
        return 60.0
    return float(minutes)


def shift_steps(anchor_at: datetime, horizon: int, step_minutes: float) -> datetime:
    """Target timestamp of ``horizon`` steps after ``anchor_at``."""
    return anchor_at + timedelta(minutes=step_minutes * float(horizon))

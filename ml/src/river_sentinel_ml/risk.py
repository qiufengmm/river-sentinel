"""Deterministic research risk rule for the River Sentinel teaching project.

**严格确定性**：纯函数、无随机、无网络、不读取时钟（时刻全部由调用方提供）。
风险等级只能由预测服务与本模块产生；后续 DS-8 的大语言模型只能**解释**结果，
不得生成或修改等级。

命名统一为 ``research_high_water``（研究性高水位阈值，train 段拟合）。它不是官方
警戒水位，也不是超警或正式预警标准；所有渲染文本必须携带「非官方标准，仅用于教学
科研辅助」的声明，并且**不得**出现「警戒 / 超警 / 预警」等字样。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Real

import numpy as np
import pandas as pd

#: 允许的等级取值；``level`` 只能是其中之一。
LEVEL_NORMAL = "normal"
LEVEL_WATCH = "watch"
LEVEL_ELEVATED = "elevated"
RISK_LEVELS: tuple[str, ...] = (LEVEL_NORMAL, LEVEL_WATCH, LEVEL_ELEVATED)

#: 数据陈旧门槛（小时）：最后观测时刻与预测锚点之差超过该值即标记为陈旧。
DEFAULT_STALE_AFTER_HOURS = 3.0

#: h1 决定 elevated；h3 / h6 决定 watch。顺序判定，命中即返回。
ELEVATED_HORIZON = 1
WATCH_HORIZONS: tuple[int, ...] = (3, 6)
DEFAULT_HORIZONS: tuple[int, ...] = (ELEVATED_HORIZON, *WATCH_HORIZONS)

DEFAULT_THRESHOLD_SOURCE = "train p90 research_high_water"
THRESHOLD_UNIT = "m"
DISCLAIMER = "非官方标准，仅用于教学科研辅助，教学科研辅助，不替代官方防汛决策。"
STALE_NOTE = "数据陈旧：当前判定的数据基础已落后于预测锚点，请先核对最新水位观测。"
LEVEL_LABELS: Mapping[str, str] = {
    LEVEL_NORMAL: "研究性高水位未见触发（预测量测三个时域均低于研究性阈值）",
    LEVEL_WATCH: "较远时域预测触及研究性高水位阈值",
    LEVEL_ELEVATED: "最近时域预测达到或超过研究性高水位阈值",
}


@dataclass(frozen=True)
class ResearchRiskVerdict:
    """Verdict of the deterministic rule.

    Attributes:
        level: One of ``normal`` / ``watch`` / ``elevated``.
        is_stale: Whether the underlying data is older than the staleness budget.
        triggered_horizons: Horizons whose forecast reaches the threshold (sorted).
        basis: Evidence mapping: threshold, threshold source, model name and version,
            forecast anchor, last observation time, data age, missing horizons and
            decision time.
    """

    level: str
    is_stale: bool
    triggered_horizons: tuple[int, ...]
    basis: Mapping[str, object]


def evaluate_research_risk(
    *,
    predictions: Mapping[int, float],
    threshold: float,
    anchor_at: pd.Timestamp,
    last_observed_at: pd.Timestamp,
    evaluated_at: pd.Timestamp | None = None,
    threshold_source: str = DEFAULT_THRESHOLD_SOURCE,
    model_name: str = "unknown",
    model_version: str = "unknown",
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> ResearchRiskVerdict:
    """Classify multi-horizon forecasts against the research high-water threshold.

    规则（顺序判定，命中即返回）：

    1. 1 步预测 ≥ 阈值 → ``elevated``；同时也达阈值的其它时域一并写入
       ``triggered_horizons``；
    2. 否则 3 步或 6 步预测 ≥ 阈值 → ``watch``；
    3. 否则 ``normal``。

    缺失或非有限的预测**不触发**，但必须写进 ``basis["missing_horizons"]``。
    数据陈旧只做**标注**（``is_stale``）：本模块不因陈旧上调或下调等级，是否降级由
    调用方（DS-8）决定。

    Raises:
        ValueError: 阈值非法、``predictions`` 为空、horizon 非正整数、
            ``anchor_at`` 早于 ``last_observed_at``，或时刻缺少时区信息。
    """
    limit = _validated_threshold(threshold)
    anchor = _validated_timestamp(anchor_at, name="anchor_at")
    observed = _validated_timestamp(last_observed_at, name="last_observed_at")
    decided = (
        anchor if evaluated_at is None else _validated_timestamp(evaluated_at, name="evaluated_at")
    )
    if observed > anchor:
        raise ValueError(
            f"anchor_at must not be earlier than last_observed_at: {anchor.isoformat()}"
        )

    steps = _validated_horizons(horizons)
    values = _validated_predictions(predictions, steps)
    stale_budget = _validated_hours(stale_after_hours)

    crossing = tuple(step for step in steps if values[step] is not None and values[step] >= limit)
    missing = tuple(step for step in steps if values[step] is None)

    if ELEVATED_HORIZON in steps and ELEVATED_HORIZON in crossing:
        level = LEVEL_ELEVATED
    elif any(step in crossing for step in WATCH_HORIZONS):
        level = LEVEL_WATCH
    else:
        level = LEVEL_NORMAL

    age_hours = (anchor - observed).total_seconds() / 3600.0
    is_stale = age_hours > stale_budget
    basis: dict[str, object] = {
        "threshold": limit,
        "threshold_unit": THRESHOLD_UNIT,
        "threshold_source": threshold_source,
        "metric": "research_high_water",
        "model_name": model_name,
        "model_version": model_version,
        "anchor_at": anchor.isoformat(),
        "data_last_observed_at": observed.isoformat(),
        "data_age_hours": _rounded(age_hours),
        "is_stale": is_stale,
        "stale_after_hours": stale_budget,
        "evaluated_at": decided.isoformat(),
        "considered_horizons": tuple(steps),
        "triggered_horizons": crossing,
        "missing_horizons": missing,
        "predicted_values": {
            f"h{step}": None if values[step] is None else _rounded(float(values[step]))
            for step in steps
        },
        "disclaimer": DISCLAIMER,
    }
    return ResearchRiskVerdict(
        level=level,
        is_stale=is_stale,
        triggered_horizons=crossing,
        basis=basis,
    )


def render_verdict(verdict: ResearchRiskVerdict) -> str:
    """Render one verdict as Chinese guidance text without official-alert wording."""
    basis = verdict.basis
    triggered = "、".join(f"h{step}" for step in verdict.triggered_horizons) or "无"
    missing = basis.get("missing_horizons") or ()
    missing_text = "、".join(f"h{step}" for step in missing) or "无"

    lines = [
        f"研究性判定等级：{verdict.level}（research_high_water）",
        LEVEL_LABELS.get(verdict.level, ""),
        f"研究性阈值：{_number(basis['threshold'])} {basis['threshold_unit']}"
        f"（来源：{basis['threshold_source']}）",
        f"触发时域：{triggered}；缺失预测不参评：{missing_text}",
        f"模型：{basis['model_name']} / {basis['model_version']}；预测锚点：{basis['anchor_at']}",
        f"数据最后观测：{basis['data_last_observed_at']}；"
        f"数据新鲜度：{_number(basis['data_age_hours'])} 小时"
        f"（陈旧门槛 {_number(basis['stale_after_hours'])} 小时）",
        f"判定时间：{basis['evaluated_at']}",
    ]
    if verdict.is_stale:
        lines.append(STALE_NOTE)
    lines.append(str(basis["disclaimer"]))
    return "\n".join(line for line in lines if line)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _validated_threshold(threshold: object) -> float:
    if isinstance(threshold, bool) or not isinstance(threshold, Real):
        raise ValueError(f"threshold must be a finite positive number, got {threshold!r}")
    value = float(threshold)
    if not np.isfinite(value) or value <= 0.0:
        raise ValueError(f"threshold must be a finite positive number, got {threshold!r}")
    return value


def _validated_hours(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"stale_after_hours must be a non-negative number, got {value!r}")
    number = float(value)
    if not np.isfinite(number) or number < 0.0:
        raise ValueError(f"stale_after_hours must be a non-negative number, got {value!r}")
    return number


def _validated_timestamp(value: object, *, name: str) -> pd.Timestamp:
    if isinstance(value, pd.Timestamp):
        stamp = value
    else:
        try:
            stamp = pd.Timestamp(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be a timestamp, got {value!r}") from error
    if stamp.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware, got {value!r}")
    return stamp


def _validated_horizons(horizons: object) -> tuple[int, ...]:
    if isinstance(horizons, str) or not isinstance(horizons, Sequence):
        raise ValueError(f"horizons must be a sequence of positive integers, got {horizons!r}")
    validated: list[int] = []
    for horizon in horizons:
        if isinstance(horizon, bool) or not isinstance(horizon, int):
            raise ValueError(f"horizon must be a positive integer, got {horizon!r}")
        if horizon <= 0:
            raise ValueError(f"horizon must be a positive integer, got {horizon!r}")
        if horizon not in validated:
            validated.append(horizon)
    if not validated:
        raise ValueError("horizons must not be empty")
    return tuple(sorted(validated))


def _validated_predictions(
    predictions: Mapping[int, float],
    horizons: Sequence[int],
) -> dict[int, float | None]:
    if not isinstance(predictions, Mapping) or not predictions:
        raise ValueError("predictions must be a non-empty mapping of horizon to level")
    for horizon in predictions:
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
            raise ValueError(f"horizon must be a positive integer, got {horizon!r}")
    values: dict[int, float | None] = {}
    for horizon in horizons:
        if horizon not in predictions:
            values[horizon] = None
            continue
        raw = predictions[horizon]
        if raw is None:
            values[horizon] = None
            continue
        if isinstance(raw, bool) or not isinstance(raw, Real):
            raise ValueError(f"prediction for horizon {horizon} must be a number, got {raw!r}")
        number = float(raw)
        values[horizon] = number if np.isfinite(number) else None
    if all(value is None for value in values.values()):
        raise ValueError("predictions must contain at least one finite value")
    return values


def _rounded(value: float) -> float:
    return float(round(value, 6))


def _number(value: object) -> str:
    number = float(value)  # type: ignore[arg-type]
    return f"{number:.6f}".rstrip("0").rstrip(".") if number == int(number) else f"{number:.6f}"

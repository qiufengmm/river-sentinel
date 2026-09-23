"""Business payloads exchanged by services, agent tools and HTTP routes (DS-8A).

约定：

- 时刻字段一律**带时区**，由 Pydantic 校验；
- 预测值不可用时写 ``None``，禁止用 ``0``、随机数或大语言模型文本填充；
- 风险等级只能由 ``river_sentinel_ml.risk.evaluate_research_risk`` 产生。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .common import (
    DISCLAIMER,
    SUPPORTED_HORIZONS,
    DataMode,
    Evidence,
    Freshness,
    ResponseStatus,
)

RiskLevel = Literal["normal", "watch", "elevated"]
ModelName = Literal["persistence", "xgboost", "ridge", "lstm"]
QualityFlag = Literal["normal", "suspected", "invalid", "imputed"]


def _require_aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("datetime must be timezone-aware")
    return value


def _validate_horizons(value: list[int]) -> list[int]:
    if not value:
        raise ValueError("horizons must not be empty")
    result: list[int] = []
    for horizon in value:
        if isinstance(horizon, bool) or horizon not in SUPPORTED_HORIZONS:
            raise ValueError(f"horizon must be one of {SUPPORTED_HORIZONS}, got {horizon!r}")
        if horizon not in result:
            result.append(horizon)
    return sorted(result)


# --------------------------------------------------------------------------- #
# 历史水位
# --------------------------------------------------------------------------- #


class WaterLevelPoint(BaseModel):
    """One usable water-level observation."""

    station_id: str
    observed_at: datetime
    water_level_m: float
    quality_flag: QualityFlag = "normal"
    is_imputed: bool = False

    _validate_observed_at = field_validator("observed_at")(_require_aware)

    @field_validator("water_level_m")
    @classmethod
    def _validate_level(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("water_level_m must be a finite number")
        return value


class HistoryResult(BaseModel):
    """History query result with data-quality notes."""

    station_id: str
    data_mode: DataMode
    points: list[WaterLevelPoint] = Field(default_factory=list)
    quality_notes: list[str] = Field(default_factory=list)
    status: ResponseStatus = "ok"
    freshness: Freshness = "unknown"
    latest_observed_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


# --------------------------------------------------------------------------- #
# 预测
# --------------------------------------------------------------------------- #


class PredictionPoint(BaseModel):
    """One horizon forecast. ``water_level_m`` is ``None`` when unavailable."""

    horizon: int
    target_at: datetime
    water_level_m: float | None = None
    source: Literal["artifact", "persistence"] = "persistence"

    _validate_target_at = field_validator("target_at")(_require_aware)


class PredictionSeries(BaseModel):
    """Prediction artifact loaded from an explicitly configured local directory."""

    model: str
    model_version: str
    as_of: datetime
    anchor_at: datetime
    points: list[PredictionPoint] = Field(default_factory=list)
    source_path: str | None = None


class ForecastRequest(BaseModel):
    """Forecast request; ``horizons`` must be a subset of ``(1, 3, 6)``."""

    station_id: str = "cata_12720"
    as_of: datetime | None = None
    horizons: list[int] = Field(default_factory=lambda: [1, 3, 6])
    model: ModelName = "persistence"
    data_mode: DataMode = "history_replay"

    _validate_as_of = field_validator("as_of")(_require_aware)
    _validate_horizons = field_validator("horizons")(_validate_horizons)


class ForecastResult(BaseModel):
    """Forecast response including the effective model and fallback reason."""

    station_id: str
    requested_model: str
    model: str
    model_version: str
    as_of: datetime
    anchor_at: datetime | None = None
    step_minutes: float | None = None
    horizons: list[int] = Field(default_factory=list)
    predictions: list[PredictionPoint] = Field(default_factory=list)
    status: ResponseStatus = "ok"
    fallback_reason: str | None = None
    freshness: Freshness = "unknown"
    is_stale: bool = False
    data_age_minutes: float | None = None
    warnings: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


# --------------------------------------------------------------------------- #
# 模型对比
# --------------------------------------------------------------------------- #


class MetricCell(BaseModel):
    """Regression metrics for one (segment, horizon, model) cell."""

    segment: str
    horizon: int
    model: str
    display_name: str
    model_version: str
    sample_count: int | None = None
    mae: float | None = None
    rmse: float | None = None
    r2: float | None = None
    nse: float | None = None
    reason: str | None = None


class EventMetricCell(BaseModel):
    """Event-level metrics; undefined cells keep ``None`` and a ``reason``."""

    segment: str
    horizon: int
    model: str
    display_name: str
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    tp: int = 0
    fp: int = 0
    fn: int = 0
    observed_event_points: int = 0
    observed_event_segments: int = 0
    predicted_event_points: int = 0
    predicted_event_segments: int | None = None
    reason: str | None = None


class ModelComparison(BaseModel):
    """Model comparison consumed from DS-7 (never recomputed here)."""

    threshold: float
    threshold_unit: str = "m"
    threshold_source: str
    primary_comparison: list[str] = Field(default_factory=list)
    primary_note: str = ""
    metrics: list[MetricCell] = Field(default_factory=list)
    event_metrics: dict[str, dict[str, dict[str, EventMetricCell]]] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    source: str
    status: ResponseStatus = "ok"
    warnings: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


# --------------------------------------------------------------------------- #
# 风险判定
# --------------------------------------------------------------------------- #


class RiskRequest(BaseModel):
    """Risk evaluation request.

    ``predictions`` 缺省时按**持久性假设**把 ``water_level`` 代入每个请求时域
    （教学演示用，会在 ``warnings`` 中显式标注）。
    """

    station_id: str = "cata_12720"
    water_level: float
    observed_at: datetime
    as_of: datetime | None = None
    horizons: list[int] = Field(default_factory=lambda: [1, 3, 6])
    predictions: dict[int, float] | None = None
    model: str = "persistence"
    model_version: str = "single_level_input"
    data_mode: DataMode = "history_replay"

    _validate_observed_at = field_validator("observed_at")(_require_aware)
    _validate_as_of = field_validator("as_of")(_require_aware)
    _validate_horizons = field_validator("horizons")(_validate_horizons)

    @field_validator("water_level")
    @classmethod
    def _validate_level(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("water_level must be a finite number")
        return value

    @field_validator("predictions")
    @classmethod
    def _validate_predictions(cls, value: dict[int, float] | None) -> dict[int, float] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("predictions must not be empty when supplied")
        for horizon, level in value.items():
            if isinstance(horizon, bool) or horizon not in SUPPORTED_HORIZONS:
                raise ValueError(f"prediction horizon must be one of {SUPPORTED_HORIZONS}")
            if level != level or level in (float("inf"), float("-inf")):
                raise ValueError(f"prediction for h{horizon} must be finite")
        return {int(key): float(val) for key, val in value.items()}


class RiskResult(BaseModel):
    """Deterministic research risk verdict (level never produced by a provider)."""

    station_id: str
    level: RiskLevel
    is_stale: bool
    threshold: float
    threshold_unit: str = "m"
    threshold_source: str
    metric_name: str = "research_high_water"
    triggered_horizons: list[int] = Field(default_factory=list)
    missing_horizons: list[int] = Field(default_factory=list)
    predicted_values: dict[str, float | None] = Field(default_factory=dict)
    anchor_at: datetime
    last_observed_at: datetime
    evaluated_at: datetime
    data_age_hours: float = 0.0
    model: str
    model_version: str
    basis_source: Literal["deterministic_rule"] = "deterministic_rule"
    basis: dict[str, Any] = Field(default_factory=dict)
    guidance: str = ""
    limitations: list[str] = Field(default_factory=list)
    status: ResponseStatus = "ok"
    warnings: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


# --------------------------------------------------------------------------- #
# 简报
# --------------------------------------------------------------------------- #


class BriefSection(BaseModel):
    """One brief section composed from structured results only."""

    heading: str
    lines: list[str] = Field(default_factory=list)


class EvidenceBundle(BaseModel):
    """Structured inputs of a brief; free-text numbers are not accepted."""

    station_id: str
    data_mode: DataMode
    latest: WaterLevelPoint | None = None
    forecast: ForecastResult | None = None
    risk: RiskResult | None = None
    comparison: ModelComparison | None = None
    tool_trace: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BriefResult(BaseModel):
    """Structured water brief."""

    title: str
    station_id: str
    data_mode: DataMode
    generated_at: datetime
    data_time: datetime | None = None
    model_version: str | None = None
    risk_level: RiskLevel | None = None
    highlights: list[str] = Field(default_factory=list)
    sections: list[BriefSection] = Field(default_factory=list)
    tool_trace: list[str] = Field(default_factory=list)
    status: ResponseStatus = "ok"
    warnings: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


class BriefRequest(BaseModel):
    """Brief generation request."""

    station_id: str = "cata_12720"
    as_of: datetime | None = None
    horizons: list[int] = Field(default_factory=lambda: [1, 3, 6])
    model: ModelName = "persistence"
    data_mode: DataMode = "history_replay"

    _validate_as_of = field_validator("as_of")(_require_aware)
    _validate_horizons = field_validator("horizons")(_validate_horizons)


# --------------------------------------------------------------------------- #
# 智能体
# --------------------------------------------------------------------------- #


class AgentChatRequest(BaseModel):
    """Agent chat request routed by the LangGraph whitelist."""

    message: str = Field(min_length=1)
    station_id: str = "cata_12720"
    as_of: datetime | None = None
    horizons: list[int] = Field(default_factory=lambda: [1, 3, 6])
    model: ModelName = "persistence"
    data_mode: DataMode = "history_replay"

    _validate_as_of = field_validator("as_of")(_require_aware)
    _validate_horizons = field_validator("horizons")(_validate_horizons)


class AgentResponse(BaseModel):
    """Agent answer with tool trace, evidence and the immutable risk verdict."""

    answer: str
    intent: str
    tool_trace: list[str] = Field(default_factory=list)
    status: ResponseStatus = "ok"
    station_id: str = "cata_12720"
    data_mode: DataMode = "history_replay"
    observed_at: datetime | None = None
    model_version: str | None = None
    provider: str = "local_demo"
    risk_verdict: RiskResult | None = None
    evidence_summaries: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


class HealthPayload(BaseModel):
    """Health-check payload describing data and provider availability."""

    service: str
    version: str
    data_mode: DataMode
    station_id: str
    sample_available: bool
    sample_path: str
    artifact_dir: str | None = None
    llm_provider: str
    external_provider_available: bool = False
    persistence_fallback_available: bool = True
    supported_horizons: list[int] = Field(default_factory=lambda: list(SUPPORTED_HORIZONS))
    research_threshold: float
    disclaimer: str = DISCLAIMER

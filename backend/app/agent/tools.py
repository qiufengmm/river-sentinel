"""Whitelisted agent tools (DS-8A).

智能体只能通过这里的五个工具访问业务能力：**不允许**大模型自行计算水位、预测值或
风险等级。工具失败时返回明确失败状态与原因，**不补造**站点、水位或风险建议。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..errors import BackendError
from ..schemas.common import ResponseStatus
from ..schemas.domain import ForecastRequest, ForecastResult
from ..services.container import ServiceContainer, compose_evidence_bundle


@dataclass(frozen=True)
class ToolResult:
    """Outcome of one whitelisted tool call."""

    name: str
    status: ResponseStatus
    payload: dict[str, Any]
    error: str | None = None


def _dump(model: Any) -> dict[str, Any]:
    """Serialize a pydantic result into a JSON-ready dict."""
    return model.model_dump(mode="json")


def _failed(name: str, status: ResponseStatus, error: str) -> ToolResult:
    return ToolResult(name=name, status=status, payload={}, error=error)


def query_water_level_history(
    container: ServiceContainer,
    *,
    station_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
    data_mode: str | None = None,
) -> ToolResult:
    """Read-only history query over the sample (history replay / local snapshot)."""
    try:
        result = container.history.history(station_id, start, end, limit, data_mode=data_mode)
    except BackendError as error:
        return _failed("query_water_level_history", error.response_status, error.message)
    summary = (
        f"断面 {result.station_id} 返回 {len(result.points)} 个观测点"
        f"（最新 {result.latest_observed_at.isoformat() if result.latest_observed_at else '无'}）"
    )
    return ToolResult(
        name="query_water_level_history",
        status=result.status,
        payload={"summary": summary, "result": _dump(result)},
    )


def _forecast_result(
    container: ServiceContainer,
    *,
    station_id: str | None = None,
    as_of: datetime | None = None,
    horizons: list[int] | None = None,
    model: str = "persistence",
    data_mode: str | None = None,
) -> ForecastResult:
    """Run the forecast service and re-raise domain errors for the caller."""
    return container.forecast.forecast(
        ForecastRequest(
            station_id=station_id or container.settings.station_id,
            as_of=as_of,
            horizons=horizons or [1, 3, 6],
            model=model,
            data_mode=data_mode or container.settings.data_mode,
        )
    )


def forecast_water_level(
    container: ServiceContainer,
    *,
    station_id: str | None = None,
    as_of: datetime | None = None,
    horizons: list[int] | None = None,
    model: str = "persistence",
    data_mode: str | None = None,
) -> ToolResult:
    """Forecast service call; missing weights fall back to the verified baseline."""
    try:
        result = _forecast_result(
            container,
            station_id=station_id,
            as_of=as_of,
            horizons=horizons,
            model=model,
            data_mode=data_mode,
        )
    except BackendError as error:
        return _failed("forecast_water_level", error.response_status, error.message)
    values = "、".join(
        f"h{point['horizon']}={point['water_level_m']}" for point in _dump(result)["predictions"]
    )
    summary = (
        f"模型 {result.model}/{result.model_version}（请求 {result.requested_model}），"
        f"状态 {result.status}，预测：{values}"
    )
    return ToolResult(
        name="forecast_water_level",
        status=result.status,
        payload={
            "summary": summary,
            "result": _dump(result),
            "fallback_reason": result.fallback_reason,
        },
    )


def compare_forecast_models(
    container: ServiceContainer,
    *,
    horizons: list[int] | None = None,
) -> ToolResult:
    """Read DS-7 published metrics; never recompute or retrain here."""
    try:
        result = container.comparison.compare(horizons)
    except BackendError as error:
        return _failed("compare_forecast_models", error.response_status, error.message)
    summary = (
        f"指标来源 {result.source}；主对照 {', '.join(result.primary_comparison)}；"
        f"阈值 {result.threshold} m；状态 {result.status}"
    )
    return ToolResult(
        name="compare_forecast_models",
        status=result.status,
        payload={"summary": summary, "result": _dump(result)},
    )


def evaluate_research_risk(
    container: ServiceContainer,
    *,
    station_id: str | None = None,
    as_of: datetime | None = None,
    horizons: list[int] | None = None,
    model: str = "persistence",
    data_mode: str | None = None,
) -> ToolResult:
    """Deterministic research risk verdict produced by ``river_sentinel_ml.risk``."""
    try:
        forecast_result = _forecast_result(
            container,
            station_id=station_id,
            as_of=as_of,
            horizons=horizons,
            model=model,
            data_mode=data_mode,
        )
        risk = container.risk.evaluate_forecast(forecast_result, data_mode=data_mode)
    except BackendError as error:
        return _failed("evaluate_research_risk", error.response_status, error.message)
    summary = (
        f"研究性判定等级 {risk.level}（阈值 {risk.threshold} m，"
        f"触发时域 {', '.join(f'h{h}' for h in risk.triggered_horizons) or '无'}）"
    )
    return ToolResult(
        name="evaluate_research_risk",
        status=risk.status,
        payload={
            "summary": summary,
            "result": _dump(risk),
            "forecast": _dump(forecast_result),
            "forecast_summary": (
                f"模型 {forecast_result.model}/{forecast_result.model_version}，"
                f"状态 {forecast_result.status}"
            ),
        },
    )


def build_water_brief(
    container: ServiceContainer,
    *,
    station_id: str | None = None,
    as_of: datetime | None = None,
    horizons: list[int] | None = None,
    model: str = "persistence",
    data_mode: str | None = None,
) -> ToolResult:
    """Structured brief composed from tool results only (no free-text numbers)."""
    try:
        bundle = compose_evidence_bundle(
            container,
            station_id=station_id,
            as_of=as_of,
            horizons=horizons,
            model=model,
            data_mode=data_mode,
            tool_trace=["query_water_level_history", "forecast_water_level"],
        )
        brief = container.brief.build(bundle)
    except BackendError as error:
        return _failed("build_water_brief", error.response_status, error.message)
    summary = f"{brief.title}（{len(brief.sections)} 个小节，状态 {brief.status}）"
    return ToolResult(
        name="build_water_brief",
        status=brief.status,
        payload={"summary": summary, "result": _dump(brief)},
    )


#: 白名单工具：路由、证据与前端都以此为准，不得新增未登记工具。
TOOL_REGISTRY: dict[str, Callable[..., ToolResult]] = {
    "query_water_level_history": query_water_level_history,
    "forecast_water_level": forecast_water_level,
    "compare_forecast_models": compare_forecast_models,
    "evaluate_research_risk": evaluate_research_risk,
    "build_water_brief": build_water_brief,
}

WHITELISTED_TOOLS: tuple[str, ...] = (
    "build_water_brief",
    "compare_forecast_models",
    "evaluate_research_risk",
    "forecast_water_level",
    "query_water_level_history",
)

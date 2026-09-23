"""HTTP routes mounted under ``Settings.api_prefix`` (default ``/api/v1``).

所有接口都返回统一外壳 :class:`ApiResponse`；``data_mode`` 只能是 ``history_replay``
或 ``local_snapshot``，证据里带观测时间、新鲜度、模型版本与工具调用摘要。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request

from ..agent.graph import run_agent
from ..agent.providers import provider_available
from ..config import Settings
from ..errors import InvalidRequestError
from ..schemas.common import (
    DATA_UNAVAILABLE,
    RESEARCH_THRESHOLD,
    SUPPORTED_HORIZONS,
    ApiResponse,
    Evidence,
    ResponseStatus,
    warning_of,
)
from ..schemas.domain import (
    AgentChatRequest,
    BriefRequest,
    ForecastRequest,
    HealthPayload,
    RiskRequest,
)
from ..services.container import ServiceContainer, compose_evidence_bundle


def _request_id(request: Request) -> str:
    """Request id assigned by the middleware (``unknown`` only if absent)."""
    return str(getattr(request.state, "request_id", "unknown"))


def _envelope(
    request: Request,
    data: Any,
    *,
    status: ResponseStatus = "ok",
    evidence: list[Evidence] | None = None,
    warnings: list[str] | None = None,
) -> ApiResponse[Any]:
    return ApiResponse(
        request_id=_request_id(request),
        status=status,
        data=data,
        evidence=list(evidence or []),
        warnings=list(warnings or []),
    )


def _parse_horizons(raw: str) -> list[int]:
    """Parse a comma separated horizon list such as ``"1,3,6"``."""
    items = [item.strip() for item in raw.split(",") if item.strip()]
    if not items:
        raise InvalidRequestError("horizons 不能为空")
    horizons: list[int] = []
    for item in items:
        try:
            value = int(item)
        except ValueError as error:
            raise InvalidRequestError(
                f"horizons 只能包含 {SUPPORTED_HORIZONS}，得到 {item!r}"
            ) from error
        if value not in SUPPORTED_HORIZONS:
            raise InvalidRequestError(f"horizons 只能包含 {SUPPORTED_HORIZONS}，得到 {value!r}")
        if value not in horizons:
            horizons.append(value)
    return sorted(horizons)


def build_router(container: ServiceContainer) -> APIRouter:
    """Build the API router for the given service container."""
    router = APIRouter()
    settings: Settings = container.settings

    @router.get("/health", response_model=ApiResponse[HealthPayload])
    def health(request: Request) -> ApiResponse[Any]:
        """Service, data and provider availability."""
        sample_available = settings.resolved_sample_path.is_file()
        payload = HealthPayload(
            service=settings.service_name,
            version=settings.service_version,
            data_mode=settings.data_mode,
            station_id=settings.station_id,
            sample_available=sample_available,
            sample_path=str(settings.sample_path),
            artifact_dir=(None if settings.artifact_dir is None else str(settings.artifact_dir)),
            llm_provider=settings.llm_provider,
            external_provider_available=provider_available(settings),
            persistence_fallback_available=sample_available,
            research_threshold=RESEARCH_THRESHOLD,
        )
        warnings: list[str] = []
        if not sample_available:
            warnings.append(
                warning_of(
                    DATA_UNAVAILABLE,
                    f"样例数据不可用：{settings.sample_path}；持久性基线亦不可用",
                )
            )
        evidence = [
            Evidence(
                source_url=settings.source_url,
                data_mode=settings.data_mode,
                freshness="unknown",
                tool_trace=["health"],
                notes=[f"研究性阈值：{RESEARCH_THRESHOLD} m（非官方标准）"],
            )
        ]
        return _envelope(
            request,
            payload,
            status="ok" if sample_available else "degraded",
            evidence=evidence,
            warnings=warnings,
        )

    @router.get("/water-level/history", response_model=ApiResponse[Any])
    def water_level_history(
        request: Request,
        station_id: str = settings.station_id,
        start: datetime | None = Query(default=None),
        end: datetime | None = Query(default=None),
        limit: int | None = Query(default=None, ge=1),
    ) -> ApiResponse[Any]:
        """Time-ordered sample observations (history replay / local snapshot)."""
        result = container.history.history(station_id, start, end, limit)
        return _envelope(
            request,
            result,
            status=result.status,
            evidence=result.evidence,
            warnings=result.warnings,
        )

    @router.post("/forecast", response_model=ApiResponse[Any])
    def forecast(request: Request, payload: ForecastRequest) -> ApiResponse[Any]:
        """Multi-horizon forecast with deterministic persistence fallback."""
        result = container.forecast.forecast(payload)
        return _envelope(
            request,
            result,
            status=result.status,
            evidence=result.evidence,
            warnings=result.warnings,
        )

    @router.get("/model-comparison", response_model=ApiResponse[Any])
    def model_comparison(
        request: Request,
        horizons: str = Query(default="1,3,6"),
    ) -> ApiResponse[Any]:
        """Published DS-7 metrics, sample counts and limitations."""
        result = container.comparison.compare(_parse_horizons(horizons))
        return _envelope(
            request,
            result,
            status=result.status,
            evidence=result.evidence,
            warnings=result.warnings,
        )

    @router.post("/risk/evaluate", response_model=ApiResponse[Any])
    def risk_evaluate(request: Request, payload: RiskRequest) -> ApiResponse[Any]:
        """Deterministic research risk verdict (never produced by a provider)."""
        result = container.risk.evaluate(payload)
        return _envelope(
            request,
            result,
            status=result.status,
            evidence=result.evidence,
            warnings=result.warnings,
        )

    @router.post("/agent/chat", response_model=ApiResponse[Any])
    def agent_chat(request: Request, payload: AgentChatRequest) -> ApiResponse[Any]:
        """LangGraph agent over the whitelisted tools."""
        result = run_agent(
            container,
            payload.message,
            station_id=payload.station_id,
            as_of=payload.as_of,
            horizons=payload.horizons,
            model=payload.model,
            data_mode=payload.data_mode,
        )
        evidence = [
            Evidence(
                source_url=settings.source_url,
                data_mode=result.data_mode,
                observed_at=result.observed_at,
                freshness="unknown",
                model_version=result.model_version,
                tool_trace=result.tool_trace,
                notes=result.evidence_summaries,
            )
        ]
        return _envelope(
            request,
            result,
            status=result.status,
            evidence=evidence,
            warnings=result.warnings,
        )

    @router.post("/brief", response_model=ApiResponse[Any])
    def brief(request: Request, payload: BriefRequest) -> ApiResponse[Any]:
        """Structured water brief built from tool results only."""
        bundle = compose_evidence_bundle(
            container,
            station_id=payload.station_id,
            as_of=payload.as_of,
            horizons=payload.horizons,
            model=payload.model,
            data_mode=payload.data_mode,
        )
        result = container.brief.build(bundle)
        return _envelope(
            request,
            result,
            status=result.status,
            evidence=result.evidence,
            warnings=result.warnings,
        )

    return router

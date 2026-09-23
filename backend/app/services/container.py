"""Service container wiring repositories and services (DS-8A)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config import Settings
from ..errors import ModelUnavailableError
from ..repositories.artifact_repository import ArtifactRepository
from ..repositories.sample_repository import SampleRepository
from ..schemas.common import MODEL_UNAVAILABLE, warning_of
from ..schemas.domain import EvidenceBundle, ForecastRequest
from .brief_service import BriefService
from .comparison_service import ComparisonService
from .forecast_service import ForecastService
from .history_service import HistoryService
from .risk_service import RiskService


@dataclass(frozen=True)
class ServiceContainer:
    """Read-only bundle of repositories and services used by routes and the agent."""

    settings: Settings
    samples: SampleRepository
    artifacts: ArtifactRepository
    history: HistoryService
    forecast: ForecastService
    comparison: ComparisonService
    risk: RiskService
    brief: BriefService


def build_container(settings: Settings | None = None) -> ServiceContainer:
    """Build the default container from settings (defaults to the sample file)."""
    resolved = settings or Settings()
    samples = SampleRepository(resolved)
    artifacts = ArtifactRepository(resolved)
    history = HistoryService(samples, resolved)
    forecast = ForecastService(samples, artifacts, resolved)
    comparison = ComparisonService(artifacts, resolved)
    risk = RiskService(resolved, forecast)
    brief = BriefService(resolved)
    return ServiceContainer(
        settings=resolved,
        samples=samples,
        artifacts=artifacts,
        history=history,
        forecast=forecast,
        comparison=comparison,
        risk=risk,
        brief=brief,
    )


def compose_evidence_bundle(
    container: ServiceContainer,
    *,
    station_id: str | None = None,
    as_of: datetime | None = None,
    horizons: list[int] | None = None,
    model: str = "persistence",
    data_mode: str | None = None,
    include_comparison: bool = True,
    tool_trace: list[str] | None = None,
) -> EvidenceBundle:
    """Collect history, forecast, risk and comparison results for a brief.

    风险判定失败（预测全部不可用）时不补造等级，只把失败原因写入 ``warnings``。
    """
    mode = data_mode or container.settings.data_mode
    station = station_id or container.settings.station_id
    requested_horizons = horizons or [1, 3, 6]

    history = container.history.history(station, None, as_of, data_mode=mode)
    forecast = container.forecast.forecast(
        ForecastRequest(
            station_id=station,
            as_of=as_of,
            horizons=requested_horizons,
            model=model,
            data_mode=mode,
        )
    )

    warnings: list[str] = list(history.warnings)
    risk = None
    try:
        risk = container.risk.evaluate_forecast(forecast, data_mode=mode)
    except ModelUnavailableError as error:
        warnings.append(warning_of(MODEL_UNAVAILABLE, f"风险判定未执行：{error}"))

    comparison = container.comparison.compare(requested_horizons) if include_comparison else None
    trace = list(tool_trace or [])
    trace.append("query_water_level_history")
    trace.append("forecast_water_level")

    return EvidenceBundle(
        station_id=station,
        data_mode=mode,
        latest=history.points[-1] if history.points else None,
        forecast=forecast,
        risk=risk,
        comparison=comparison,
        tool_trace=trace,
        warnings=warnings,
    )

"""History query service over the small sample (DS-8A)."""

from __future__ import annotations

from datetime import datetime

from ..config import Settings
from ..errors import InvalidRequestError
from ..repositories.sample_repository import SampleRepository
from ..schemas.common import (
    DATA_UNAVAILABLE,
    DataMode,
    Evidence,
    ResponseStatus,
    warning_of,
)
from ..schemas.domain import HistoryResult
from ..timeutils import age_minutes, ensure_aware, freshness_of


class HistoryService:
    """Serves time-ordered observations for the configured target station."""

    def __init__(self, samples: SampleRepository, settings: Settings) -> None:
        self._samples = samples
        self._settings = settings

    def history(
        self,
        station_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
        data_mode: DataMode | None = None,
    ) -> HistoryResult:
        """Query observations in ``[start, end]``.

        Naive timestamps are localized with ``Settings.timezone``; the result is always
        sorted ascending. An empty range yields ``status="unavailable"`` with an explicit
        warning instead of fabricated points.
        """
        station = station_id or self._settings.station_id
        mode = data_mode or self._settings.data_mode
        if start is not None:
            start = ensure_aware(start, self._settings.timezone)
        if end is not None:
            end = ensure_aware(end, self._settings.timezone)
        if limit is not None and limit <= 0:
            raise InvalidRequestError(f"limit must be a positive integer, got {limit!r}")

        points = self._samples.history(station, start, end)
        if limit is not None:
            points = points[-limit:]

        warnings: list[str] = []
        status: ResponseStatus = "ok" if points else "unavailable"
        if not points:
            warnings.append(
                warning_of(
                    DATA_UNAVAILABLE,
                    f"该时间范围内没有可用观测（断面 {station}，start={start}，end={end}）",
                )
            )

        latest = self._samples.last_observed_at()
        latest_point = points[-1].observed_at if points else None
        reference = end or latest_point or latest
        if latest is not None and reference is not None:
            freshness = freshness_of(
                age_minutes(latest, reference), self._settings.stale_after_minutes
            )
        else:
            freshness = "unknown"

        evidence = [
            Evidence(
                source_url=self._settings.source_url,
                data_mode=mode,
                observed_at=latest_point,
                freshness=freshness,
                model_version=None,
                tool_trace=[f"query_water_level_history:station={station}:points={len(points)}"],
                notes=self._samples.quality_notes,
            )
        ]
        return HistoryResult(
            station_id=station,
            data_mode=mode,
            points=points,
            quality_notes=self._samples.quality_notes,
            status=status,
            freshness=freshness,
            latest_observed_at=latest_point,
            warnings=warnings,
            evidence=evidence,
        )

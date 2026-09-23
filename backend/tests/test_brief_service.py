"""简报服务：只从结构化结果生成（DS-8A）。

覆盖点：简报标题与小节、数据时间取自结果而非系统时钟、空证据显式失败。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from backend.app.errors import DataUnavailableError
from backend.app.schemas.domain import EvidenceBundle, WaterLevelPoint
from backend.app.services.container import ServiceContainer, compose_evidence_bundle

TIMEZONE = ZoneInfo("Asia/Shanghai")

FORBIDDEN_WORDS = ("警戒", "超警", "预警")


def test_brief_is_built_from_tool_results(container: ServiceContainer) -> None:
    bundle = compose_evidence_bundle(container, horizons=[1, 3, 6])
    brief = container.brief.build(bundle)

    assert "历史回放" in brief.title
    assert brief.data_mode == "history_replay"
    assert brief.risk_level is not None
    assert len(brief.sections) >= 3
    assert [section.heading for section in brief.sections][0] == "数据与水情"
    assert brief.generated_at == bundle.forecast.as_of
    assert brief.model_version == bundle.forecast.model_version
    assert "query_water_level_history" in brief.tool_trace
    assert brief.status in {"ok", "degraded"}


def test_brief_never_uses_official_wording(container: ServiceContainer) -> None:
    bundle = compose_evidence_bundle(container)
    brief = container.brief.build(bundle)
    rendered = brief.model_dump_json()
    for word in FORBIDDEN_WORDS:
        assert word not in rendered
    assert "不替代官方防汛决策" in rendered


def test_brief_without_evidence_fails(container: ServiceContainer) -> None:
    bundle = EvidenceBundle(station_id=container.settings.station_id, data_mode="history_replay")
    with pytest.raises(DataUnavailableError):
        container.brief.build(bundle)


def test_brief_with_observation_only(container: ServiceContainer) -> None:
    bundle = EvidenceBundle(
        station_id=container.settings.station_id,
        data_mode="local_snapshot",
        latest=WaterLevelPoint(
            station_id=container.settings.station_id,
            observed_at=datetime(2026, 1, 1, 11, tzinfo=TIMEZONE),
            water_level_m=3.74,
        ),
    )
    brief = container.brief.build(bundle)
    assert brief.data_mode == "local_snapshot"
    assert "本地快照" in brief.title
    assert len(brief.sections) == 1
    assert brief.generated_at == bundle.latest.observed_at


def test_brief_keeps_forecast_warnings(container: ServiceContainer) -> None:
    last = container.samples.last_observed_at()
    assert last is not None
    bundle = compose_evidence_bundle(container, as_of=last)
    assert bundle.warnings == []
    brief = container.brief.build(bundle)
    assert "STALE_SNAPSHOT" not in "\n".join(brief.warnings)

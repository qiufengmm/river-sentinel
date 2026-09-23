"""预测服务：持久性基线、模型缺失回退、陈旧快照与不可用状态（DS-8A）。

覆盖点：非法 horizon、模型缺失与持久性回退、陈旧快照、历史不足不补造。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from backend.app.errors import DataUnavailableError, InvalidRequestError
from backend.app.schemas.common import PERSISTENCE_MODEL, PERSISTENCE_VERSION
from backend.app.schemas.domain import ForecastRequest
from backend.app.services.container import ServiceContainer

TIMEZONE = ZoneInfo("Asia/Shanghai")


def test_persistence_forecast_matches_shifted_observations(container: ServiceContainer) -> None:
    series = container.samples.level_series(None)
    result = container.forecast.forecast(ForecastRequest())
    values = {point.horizon: point.water_level_m for point in result.predictions}

    assert result.status == "ok"
    assert result.model == PERSISTENCE_MODEL
    assert result.model_version == PERSISTENCE_VERSION
    assert result.fallback_reason is None
    assert result.horizons == [1, 3, 6]
    assert values[1] == pytest.approx(float(series.iloc[-2]))
    assert values[3] == pytest.approx(float(series.iloc[-4]))
    assert values[6] == pytest.approx(float(series.iloc[-7]))


def test_horizon_subset_request(container: ServiceContainer) -> None:
    result = container.forecast.forecast(ForecastRequest(horizons=[3]))
    assert [point.horizon for point in result.predictions] == [3]
    assert result.horizons == [3]


def test_missing_model_artifact_falls_back_to_persistence(container: ServiceContainer) -> None:
    result = container.forecast.forecast(ForecastRequest(model="xgboost"))
    assert result.status == "degraded"
    assert result.fallback_reason == "model_artifact_unavailable"
    assert result.model == PERSISTENCE_MODEL
    assert result.requested_model == "xgboost"
    assert result.model_version == PERSISTENCE_VERSION
    assert any("MODEL_UNAVAILABLE" in warning for warning in result.warnings)
    assert all(point.water_level_m is not None for point in result.predictions)


def test_configured_artifact_is_used_when_present(tmp_path, settings) -> None:
    """当显式配置产物目录且文件存在时，不再回退持久性基线。"""
    from backend.app.services.container import build_container  # noqa: PLC0415

    configured = settings.model_copy(update={"artifact_dir": tmp_path})
    (tmp_path / "predictions").mkdir()
    for horizon, level in ((1, 3.91), (3, 3.82), (6, 3.77)):
        (tmp_path / "predictions" / f"xgboost_h{horizon}.json").write_text(
            '{"model": "xgboost", "model_version": "ds6-e1", '
            '"as_of": "2026-01-01T11:00:00+08:00", "anchor_at": "2026-01-01T11:00:00+08:00", '
            f'"points": [{{"horizon": {horizon}, "target_at": "2026-01-01T12:00:00+08:00", '
            f'"water_level_m": {level}}}]}}',
            encoding="utf-8",
        )
    wired = build_container(configured)
    result = wired.forecast.forecast(ForecastRequest(model="xgboost"))
    assert result.status == "ok"
    assert result.model == "xgboost"
    assert result.model_version == "ds6-e1"
    assert {point.horizon: point.water_level_m for point in result.predictions} == {
        1: 3.91,
        3: 3.82,
        6: 3.77,
    }


def test_stale_snapshot_is_flagged(container: ServiceContainer) -> None:
    last = container.samples.last_observed_at()
    assert last is not None
    result = container.forecast.forecast(ForecastRequest(as_of=last + timedelta(hours=10)))
    assert result.is_stale is True
    assert result.freshness == "stale"
    assert result.data_age_minutes is not None and result.data_age_minutes > 180
    assert any("STALE_SNAPSHOT" in warning for warning in result.warnings)
    assert result.status == "degraded"


def test_fresh_snapshot_stays_ok(container: ServiceContainer) -> None:
    last = container.samples.last_observed_at()
    assert last is not None
    result = container.forecast.forecast(ForecastRequest(as_of=last))
    assert result.is_stale is False
    assert result.freshness == "fresh"


def test_insufficient_history_reports_unavailable(container: ServiceContainer) -> None:
    series = container.samples.level_series(None)
    as_of = series.index[0].to_pydatetime()
    result = container.forecast.forecast(ForecastRequest(as_of=as_of))
    assert result.status == "unavailable"
    assert result.fallback_reason == "insufficient_history"
    assert all(point.water_level_m is None for point in result.predictions)


def test_as_of_before_first_observation_is_unavailable(container: ServiceContainer) -> None:
    with pytest.raises(DataUnavailableError):
        container.forecast.forecast(ForecastRequest(as_of=datetime(2000, 1, 1, tzinfo=TIMEZONE)))


def test_unknown_station_is_rejected(container: ServiceContainer) -> None:
    with pytest.raises(InvalidRequestError):
        container.forecast.forecast(ForecastRequest(station_id="other"))


def test_invalid_horizon_is_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        ForecastRequest(horizons=[2])
    with pytest.raises(ValidationError):
        ForecastRequest(horizons=[])


def test_evidence_contains_mode_time_and_model(container: ServiceContainer) -> None:
    result = container.forecast.forecast(ForecastRequest())
    evidence = result.evidence[0]
    assert evidence.data_mode in {"history_replay", "local_snapshot"}
    assert evidence.observed_at is not None
    assert evidence.model_version == PERSISTENCE_VERSION
    assert any("forecast_water_level" in item for item in evidence.tool_trace)

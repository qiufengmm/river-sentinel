"""风险判定服务：只调用既有确定性规则（DS-8A）。

覆盖点：阈值口径、等级来源不可覆盖、陈旧标注、缺失时用持久性假设的显式标注。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.errors import InvalidRequestError, ModelUnavailableError
from backend.app.schemas.common import RESEARCH_THRESHOLD, RESEARCH_THRESHOLD_SOURCE
from backend.app.schemas.domain import ForecastRequest, RiskRequest
from backend.app.services.container import ServiceContainer

FORBIDDEN_WORDS = ("警戒", "超警", "预警")


def test_elevated_when_h1_reaches_threshold(container: ServiceContainer) -> None:
    result = container.risk.evaluate(
        RiskRequest(
            water_level=7.20,
            observed_at=datetime(2022, 9, 30, 17, tzinfo=timezone.utc),
        )
    )
    assert result.threshold == RESEARCH_THRESHOLD == 7.18
    assert result.threshold_source == RESEARCH_THRESHOLD_SOURCE
    assert result.level == "elevated"
    assert result.triggered_horizons == [1, 3, 6]
    assert result.basis_source == "deterministic_rule"
    assert result.status == "ok"


def test_normal_when_all_horizons_below_threshold(container: ServiceContainer) -> None:
    result = container.risk.evaluate(
        RiskRequest(
            water_level=3.50,
            observed_at=datetime(2022, 9, 30, 17, tzinfo=timezone.utc),
        )
    )
    assert result.level == "normal"
    assert result.triggered_horizons == []


def test_watch_when_only_far_horizon_crosses(container: ServiceContainer) -> None:
    result = container.risk.evaluate(
        RiskRequest(
            water_level=3.50,
            observed_at=datetime(2022, 9, 30, 17, tzinfo=timezone.utc),
            predictions={1: 7.00, 3: 7.20, 6: 7.00},
        )
    )
    assert result.level == "watch"
    assert result.triggered_horizons == [3]


def test_single_level_assumption_is_annotated(container: ServiceContainer) -> None:
    result = container.risk.evaluate(
        RiskRequest(
            water_level=7.20,
            observed_at=datetime(2022, 9, 30, 17, tzinfo=timezone.utc),
        )
    )
    assert any("SINGLE_LEVEL_PERSISTENCE_ASSUMPTION" in item for item in result.warnings)
    assert result.model == "persistence"
    assert result.model_version == "single_level_input"


def test_explicit_predictions_keep_caller_model(container: ServiceContainer) -> None:
    result = container.risk.evaluate(
        RiskRequest(
            water_level=3.50,
            observed_at=datetime(2022, 9, 30, 17, tzinfo=timezone.utc),
            predictions={1: 7.30},
            model="xgboost",
            model_version="ds6-e1",
        )
    )
    assert result.model == "xgboost"
    assert result.model_version == "ds6-e1"
    assert result.level == "elevated"
    assert result.missing_horizons == [3, 6]


def test_guidance_never_uses_official_wording(container: ServiceContainer) -> None:
    result = container.risk.evaluate(
        RiskRequest(
            water_level=7.20,
            observed_at=datetime(2022, 9, 30, 17, tzinfo=timezone.utc),
        )
    )
    assert result.guidance
    for word in FORBIDDEN_WORDS:
        assert word not in result.guidance
        assert word not in result.basis_source
    assert "研究性" in result.guidance


def test_stale_data_is_flagged_without_changing_level(container: ServiceContainer) -> None:
    observed = datetime(2022, 9, 30, 7, tzinfo=timezone.utc)
    result = container.risk.evaluate(
        RiskRequest(
            water_level=7.20,
            observed_at=observed,
            as_of=observed + timedelta(hours=10),
        )
    )
    assert result.is_stale is True
    assert result.status == "degraded"
    assert result.level == "elevated"
    assert any("STALE_SNAPSHOT" in item for item in result.warnings)
    assert any("MISSING_HORIZONS" not in item for item in result.warnings)


def test_unknown_station_is_rejected(container: ServiceContainer) -> None:
    with pytest.raises(InvalidRequestError):
        container.risk.evaluate(
            RiskRequest(
                station_id="other",
                water_level=3.5,
                observed_at=datetime(2022, 9, 30, 17, tzinfo=timezone.utc),
            )
        )


def test_as_of_before_observation_is_rejected(container: ServiceContainer) -> None:
    observed = datetime(2022, 9, 30, 17, tzinfo=timezone.utc)
    with pytest.raises(InvalidRequestError):
        container.risk.evaluate(
            RiskRequest(
                water_level=3.5,
                observed_at=observed,
                as_of=observed - timedelta(hours=1),
            )
        )


def test_evaluate_forecast_uses_effective_model(container: ServiceContainer) -> None:
    forecast = container.forecast.forecast(ForecastRequest(model="xgboost"))
    result = container.risk.evaluate_forecast(forecast)
    assert result.model == "persistence"
    assert result.model_version == "ds5b-hourly-baseline-1"
    assert result.level == "normal"
    assert result.anchor_at is not None


def test_evaluate_forecast_rejects_unavailable_predictions(container: ServiceContainer) -> None:
    series = container.samples.level_series(None)
    forecast = container.forecast.forecast(ForecastRequest(as_of=series.index[0].to_pydatetime()))
    with pytest.raises(ModelUnavailableError):
        container.risk.evaluate_forecast(forecast)

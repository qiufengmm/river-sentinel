"""统一响应外壳、证据与请求校验（DS-8A）。

覆盖点：``data_mode`` 不允许 ``realtime``、时刻必须带时区、horizon 只允许 1/3/6。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.schemas.common import (
    DISCLAIMER,
    RESEARCH_THRESHOLD,
    SUPPORTED_HORIZONS,
    ApiResponse,
    Evidence,
    warning_of,
)
from backend.app.schemas.domain import (
    ForecastRequest,
    PredictionPoint,
    RiskRequest,
    WaterLevelPoint,
)


def test_envelope_defaults() -> None:
    envelope = ApiResponse(request_id="rid-1")
    assert envelope.status == "ok"
    assert envelope.data is None
    assert envelope.evidence == []
    assert envelope.warnings == []
    payload = envelope.model_dump(mode="json")
    assert set(payload) == {"request_id", "status", "data", "evidence", "warnings"}


def test_envelope_carries_data_and_warnings() -> None:
    envelope = ApiResponse(request_id="rid-2", status="degraded", data={"model": "persistence"})
    assert envelope.data == {"model": "persistence"}


def test_evidence_rejects_realtime_data_mode() -> None:
    with pytest.raises(ValidationError):
        Evidence(data_mode="realtime")


def test_evidence_requires_timezone_aware_observation() -> None:
    with pytest.raises(ValidationError):
        Evidence(observed_at=datetime(2026, 1, 1, 11, 0))


def test_evidence_accepts_supported_data_modes() -> None:
    for mode in ("history_replay", "local_snapshot"):
        assert Evidence(data_mode=mode).data_mode == mode


def test_warning_format() -> None:
    assert warning_of("INVALID_REQUEST", "bad") == "INVALID_REQUEST: bad"


def test_forecast_request_defaults_and_horizon_dedup() -> None:
    request = ForecastRequest(horizons=[6, 1, 3, 3])
    assert request.horizons == [1, 3, 6]
    assert request.model == "persistence"
    assert request.data_mode == "history_replay"


@pytest.mark.parametrize("horizon", [[2], [0], [7], [], [1, 4]])
def test_forecast_request_rejects_invalid_horizons(horizon: list[int]) -> None:
    with pytest.raises(ValidationError):
        ForecastRequest(horizons=horizon)


def test_forecast_request_rejects_naive_as_of() -> None:
    with pytest.raises(ValidationError):
        ForecastRequest(as_of=datetime(2026, 1, 1, 11, 0))


def test_water_level_point_rejects_non_finite_level() -> None:
    with pytest.raises(ValidationError):
        WaterLevelPoint(
            station_id="cata_12720",
            observed_at=datetime(2026, 1, 1, 11, tzinfo=timezone.utc),
            water_level_m=float("nan"),
        )


def test_risk_request_rejects_invalid_prediction_horizon() -> None:
    with pytest.raises(ValidationError):
        RiskRequest(
            water_level=7.0,
            observed_at=datetime(2026, 1, 1, 11, tzinfo=timezone.utc),
            predictions={2: 7.0},
        )


def test_supported_horizons_and_threshold_contract() -> None:
    assert SUPPORTED_HORIZONS == (1, 3, 6)
    assert RESEARCH_THRESHOLD == 7.18
    assert "不替代官方防汛决策" in DISCLAIMER


def test_prediction_point_allows_missing_value() -> None:
    point = PredictionPoint(horizon=1, target_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
    assert point.water_level_m is None

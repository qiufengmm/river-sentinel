"""HTTP 契约测试：统一外壳、错误码与 ``request_id``（DS-8A）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app

PREFIX = "/api/v1"


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as test_client:
        yield test_client


def _envelope_keys(payload: dict) -> set[str]:
    return set(payload)


def test_health_reports_sample_and_provider(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/health")
    assert response.status_code == 200
    payload = response.json()
    assert _envelope_keys(payload) == {"request_id", "status", "data", "evidence", "warnings"}
    assert payload["status"] == "ok"
    assert payload["data"]["sample_available"] is True
    assert payload["data"]["data_mode"] == "history_replay"
    assert payload["data"]["llm_provider"] == "local"
    assert payload["data"]["external_provider_available"] is False
    assert payload["data"]["research_threshold"] == 7.18


def test_history_is_ascending_with_evidence(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/water-level/history")
    assert response.status_code == 200
    payload = response.json()
    data = payload["data"]
    times = [point["observed_at"] for point in data["points"]]
    assert times == sorted(times)
    assert payload["evidence"][0]["data_mode"] == "history_replay"
    assert payload["evidence"][0]["observed_at"] is not None


def test_unknown_station_returns_error_envelope(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/water-level/history", params={"station_id": "other"})
    assert response.status_code == 400
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["data"] is None
    assert payload["request_id"]
    assert any("INVALID_REQUEST" in item for item in payload["warnings"])


def test_inverted_range_returns_error(client: TestClient) -> None:
    response = client.get(
        f"{PREFIX}/water-level/history",
        params={"start": "2026-01-01T05:00:00+08:00", "end": "2026-01-01T01:00:00+08:00"},
    )
    assert response.status_code == 400
    assert any("INVALID_REQUEST" in item for item in response.json()["warnings"])


def test_forecast_default_uses_persistence(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/forecast", json={})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["data"]["model"] == "persistence"
    assert payload["data"]["horizons"] == [1, 3, 6]
    assert len(payload["data"]["predictions"]) == 3


def test_forecast_without_weights_is_degraded(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/forecast", json={"model": "xgboost"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["data"]["fallback_reason"] == "model_artifact_unavailable"
    assert payload["data"]["model"] == "persistence"
    assert payload["data"]["model_version"] == "ds5b-hourly-baseline-1"


def test_forecast_invalid_horizon_is_rejected(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/forecast", json={"horizons": [2]})
    assert response.status_code == 400
    assert any("INVALID_REQUEST" in item for item in response.json()["warnings"])


def test_model_comparison_returns_null_event_metrics(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/model-comparison")
    assert response.status_code == 200
    payload = response.json()
    data = payload["data"]
    assert data["threshold"] == 7.18
    assert data["event_metrics"]["test"]["h1"]["xgboost_e1"]["precision"] is None
    assert data["event_metrics"]["test"]["h1"]["xgboost_e1"]["reason"] == "no_observed_events"
    assert any("COMPARISON_ARTIFACT_UNAVAILABLE" in item for item in payload["warnings"])


def test_model_comparison_rejects_invalid_horizon(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/model-comparison", params={"horizons": "1,9"})
    assert response.status_code == 400
    assert any("INVALID_REQUEST" in item for item in response.json()["warnings"])


def test_risk_evaluate_uses_deterministic_rule(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/risk/evaluate",
        json={"water_level": 7.2, "observed_at": "2022-09-30T17:00:00+00:00"},
    )
    assert response.status_code == 200
    payload = response.json()
    data = payload["data"]
    assert data["level"] == "elevated"
    assert data["threshold"] == 7.18
    assert data["basis_source"] == "deterministic_rule"
    assert "不替代官方防汛决策" in data["disclaimer"]


def test_agent_chat_keeps_deterministic_verdict(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/agent/chat", json={"message": "请判断研究性风险"})
    assert response.status_code == 200
    payload = response.json()
    data = payload["data"]
    assert data["risk_verdict"]["basis_source"] == "deterministic_rule"
    assert set(data["tool_trace"]) <= {
        "query_water_level_history",
        "forecast_water_level",
        "compare_forecast_models",
        "evaluate_research_risk",
        "build_water_brief",
    }
    assert "不替代官方防汛决策" in data["answer"]
    assert payload["evidence"][0]["tool_trace"] == data["tool_trace"]


def test_brief_endpoint(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/brief", json={})
    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["sections"]
    assert "历史回放" in payload["data"]["title"]
    assert payload["data"]["risk_level"] in {"normal", "watch", "elevated"}


def test_request_id_is_unique_and_echoed(client: TestClient) -> None:
    first = client.get(f"{PREFIX}/health")
    second = client.get(f"{PREFIX}/health")
    assert first.json()["request_id"] != second.json()["request_id"]
    assert first.headers["X-Request-ID"] == first.json()["request_id"]

"""端到端闭环：历史查询 → 预测 → 模型对比 → 风险判定 → 智能体回答 → 简报。"""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from backend.app.main import create_app
from backend.app.schemas.domain import ForecastRequest, RiskRequest
from backend.app.services.container import ServiceContainer, build_container

PREFIX = "/api/v1"


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_http_closed_loop(client: TestClient) -> None:
    history = client.get(f"{PREFIX}/water-level/history").json()
    assert history["status"] == "ok"
    points = history["data"]["points"]
    assert points and [point["observed_at"] for point in points] == sorted(
        point["observed_at"] for point in points
    )

    forecast = client.post(f"{PREFIX}/forecast", json={"model": "xgboost"}).json()
    assert forecast["data"]["model"] == "persistence"
    assert forecast["status"] == "degraded"

    comparison = client.get(f"{PREFIX}/model-comparison").json()
    assert comparison["data"]["metrics"]

    risk = client.post(
        f"{PREFIX}/risk/evaluate",
        json={
            "water_level": points[-1]["water_level_m"],
            "observed_at": points[-1]["observed_at"],
        },
    ).json()
    assert risk["data"]["level"] == "normal"

    chat = client.post(f"{PREFIX}/agent/chat", json={"message": "请判断研究性风险"}).json()
    assert chat["data"]["risk_verdict"]["level"] == "normal"
    assert chat["data"]["risk_verdict"]["basis_source"] == "deterministic_rule"
    assert "历史回放" in chat["data"]["answer"]

    brief = client.post(f"{PREFIX}/brief", json={}).json()
    assert brief["status"] in {"ok", "degraded"}
    assert brief["data"]["risk_level"] == "normal"

    for payload in (history, forecast, comparison, risk, chat, brief):
        for evidence in payload["evidence"]:
            assert evidence["data_mode"] in {"history_replay", "local_snapshot"}
            assert evidence["data_mode"] != "realtime"


def test_service_closed_loop_matches_http() -> None:
    container: ServiceContainer = build_container()
    history = container.history.history(container.settings.station_id)
    assert history.points

    forecast = container.forecast.forecast(ForecastRequest(model="xgboost"))
    assert forecast.model == "persistence"

    latest = history.points[-1]
    risk = container.risk.evaluate(
        RiskRequest(water_level=latest.water_level_m, observed_at=latest.observed_at)
    )
    assert risk.level == "normal"

    from_forcast = container.risk.evaluate_forecast(forecast)
    assert from_forcast.level == "normal"
    assert from_forcast.model_version == forecast.model_version

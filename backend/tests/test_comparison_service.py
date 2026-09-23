"""模型对比服务：DS-7 指标只读消费与零事件 null（DS-8A）。

覆盖点：test 段 p90 下无真实事件时事件指标为 ``null`` + ``no_observed_events``。
"""

from __future__ import annotations

import json

from backend.app.errors import InvalidRequestError

from backend.app.schemas.common import RESEARCH_THRESHOLD
import pytest


def test_threshold_and_primary_comparison(container) -> None:
    result = container.comparison.compare()
    assert result.threshold == RESEARCH_THRESHOLD
    assert result.threshold == 7.18
    assert set(result.primary_comparison) == {"xgboost_e1", "persistence"}
    assert "Ridge" in result.primary_note and "LSTM" in result.primary_note


def test_sample_counts_are_reported(container) -> None:
    result = container.comparison.compare([1])
    counts = {cell.model: cell.sample_count for cell in result.metrics}
    assert counts["persistence"] == 144
    assert counts["xgboost_e1"] == 144
    assert counts["lstm_e2"] == 144


def test_test_segment_metrics_match_published_values(container) -> None:
    result = container.comparison.compare([3])
    cells = {(cell.model): cell for cell in result.metrics}
    assert cells["persistence"].mae == pytest.approx(0.107542)
    assert cells["persistence"].rmse == pytest.approx(0.233371)
    assert cells["persistence"].r2 == pytest.approx(0.628046)
    assert cells["xgboost_e1"].sample_count == 118


def test_no_observed_events_keeps_metrics_null(container) -> None:
    result = container.comparison.compare([1])
    cell = result.event_metrics["test"]["h1"]["xgboost_e1"]
    assert cell.precision is None
    assert cell.recall is None
    assert cell.f1 is None
    assert cell.reason == "no_observed_events"
    assert cell.observed_event_points == 0
    assert cell.predicted_event_points == 1


def test_event_grid_covers_all_horizons_and_models(container) -> None:
    result = container.comparison.compare()
    assert set(result.event_metrics["test"]) == {"h1", "h3", "h6"}
    assert len(result.event_metrics["test"]["h6"]) == 7
    assert all(
        cell.reason == "no_observed_events"
        for models in result.event_metrics["test"].values()
        for cell in models.values()
    )


def test_limitations_are_preserved(container) -> None:
    result = container.comparison.compare()
    joined = "\n".join(result.limitations)
    assert "非官方警戒" in joined
    assert "no_observed_events" in joined
    assert "教学科研" in joined


def test_missing_artifact_marks_degraded(container) -> None:
    result = container.comparison.compare()
    assert result.status == "degraded"
    assert any("COMPARISON_ARTIFACT_UNAVAILABLE" in warning for warning in result.warnings)
    assert "hourly-error-and-events.md" in result.source


def test_configured_artifact_is_preferred(tmp_path, settings) -> None:
    from backend.app.services.container import build_container  # noqa: PLC0415

    payload = {
        "source": "configured",
        "threshold": 7.18,
        "metrics": [
            {
                "segment": "test",
                "horizon": 1,
                "model": "xgboost_e1",
                "display_name": "XGBoost E1",
                "model_version": "ds6-e1",
                "sample_count": 144,
                "mae": 0.08,
                "rmse": 0.15,
                "r2": 0.81,
                "nse": 0.81,
            }
        ],
        "event_metrics": {
            "test": {
                "h1": {
                    "xgboost_e1": {
                        "observed_event_points": 0,
                        "reason": "no_observed_events",
                    }
                }
            }
        },
        "limitations": ["configured limitation"],
    }
    (tmp_path / "model_comparison.json").write_text(json.dumps(payload), encoding="utf-8")
    wired = build_container(settings.model_copy(update={"artifact_dir": tmp_path}))
    result = wired.comparison.compare([1])
    assert result.status == "ok"
    assert result.source == "configured"
    assert result.metrics[0].mae == 0.08
    assert result.event_metrics["test"]["h1"]["xgboost_e1"].precision is None


def test_invalid_horizon_is_rejected(container) -> None:
    with pytest.raises(InvalidRequestError):
        container.comparison.compare([2])
    with pytest.raises(InvalidRequestError):
        container.comparison.compare([])

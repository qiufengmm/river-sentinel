"""Contract tests for the deterministic research risk rule.

风险等级必须由确定性代码产生；名称统一为 research_high_water（研究性阈值），
非官方标准，仅用于教学科研辅助。
"""

from datetime import timedelta

import pandas as pd
import pytest

from river_sentinel_ml.risk import (
    RISK_LEVELS,
    ResearchRiskVerdict,
    evaluate_research_risk,
    render_verdict,
)

TIMEZONE = "Asia/Shanghai"
THRESHOLD = 7.18
ANCHOR = pd.Timestamp("2022-09-22T12:00:00", tz=TIMEZONE)
FRESH_OBSERVATION = pd.Timestamp("2022-09-22T11:00:00", tz=TIMEZONE)
FORBIDDEN_WORDS = ("警戒", "超警", "预警", "正式预警")


def _verdict(predictions, **kwargs) -> ResearchRiskVerdict:
    options = {
        "predictions": predictions,
        "threshold": THRESHOLD,
        "anchor_at": ANCHOR,
        "last_observed_at": FRESH_OBSERVATION,
        "evaluated_at": ANCHOR,
        "threshold_source": "train p90 research_high_water",
        "model_name": "xgboost",
        "model_version": "E2-h1-20260921",
    }
    options.update(kwargs)
    return evaluate_research_risk(**options)


def test_elevated_when_h1_reaches_the_threshold():
    verdict = _verdict({1: 7.18, 3: 7.0, 6: 6.9})

    assert verdict.level == "elevated"
    assert verdict.triggered_horizons == (1,)


def test_watch_when_only_later_horizons_reach_the_threshold():
    verdict = _verdict({1: 7.0, 3: 7.2, 6: 7.5})

    assert verdict.level == "watch"
    assert verdict.triggered_horizons == (3, 6)


def test_normal_when_no_horizon_reaches_the_threshold():
    verdict = _verdict({1: 6.5, 3: 6.6, 6: 7.17})

    assert verdict.level == "normal"
    assert verdict.triggered_horizons == ()


def test_all_crossing_horizons_are_listed_for_elevated():
    verdict = _verdict({1: 7.4, 3: 7.3, 6: 7.19})

    assert verdict.level == "elevated"
    assert verdict.triggered_horizons == (1, 3, 6)


def test_missing_predictions_never_trigger_but_are_recorded():
    verdict = _verdict({1: float("nan"), 3: 7.5, 6: None})

    assert verdict.level == "watch"
    assert verdict.triggered_horizons == (3,)
    assert verdict.basis["missing_horizons"] == (1, 6)


def test_stale_flag_follows_the_three_hour_rule_including_its_boundary():
    exact = _verdict({1: 6.0}, last_observed_at=ANCHOR - timedelta(hours=3))
    assert exact.is_stale is False

    older = _verdict({1: 6.0}, last_observed_at=ANCHOR - timedelta(hours=3, minutes=1))
    assert older.is_stale is True
    assert float(older.basis["data_age_hours"]) > 3.0

    # 陈旧只做标注，不改变等级本身
    stale_elevated = _verdict({1: 7.5}, last_observed_at=ANCHOR - timedelta(hours=9))
    assert stale_elevated.is_stale is True
    assert stale_elevated.level == "elevated"


def test_basis_carries_threshold_source_model_and_freshness():
    verdict = _verdict({1: 7.2}, last_observed_at=ANCHOR - timedelta(hours=1))

    assert verdict.basis["threshold"] == pytest.approx(THRESHOLD)
    assert verdict.basis["threshold_source"] == "train p90 research_high_water"
    assert verdict.basis["model_name"] == "xgboost"
    assert verdict.basis["model_version"] == "E2-h1-20260921"
    assert verdict.basis["anchor_at"] == ANCHOR.isoformat()
    assert verdict.basis["data_last_observed_at"] == (ANCHOR - timedelta(hours=1)).isoformat()
    assert float(verdict.basis["data_age_hours"]) == pytest.approx(1.0)
    assert verdict.basis["is_stale"] is False


def test_render_verdict_avoids_forbidden_official_wording():
    verdict = _verdict({1: 7.5, 3: 7.2}, last_observed_at=ANCHOR - timedelta(hours=5))

    text = render_verdict(verdict)

    for word in FORBIDDEN_WORDS:
        assert word not in text
    assert "research_high_water" in text
    assert "非官方标准" in text
    assert "仅用于教学科研辅助" in text
    assert "elevated" in text
    assert "数据陈旧" in text


def test_verdict_is_deterministic_for_identical_inputs():
    first = _verdict({1: 7.19, 3: 7.0, 6: 6.5})
    second = _verdict({1: 7.19, 3: 7.0, 6: 6.5})

    assert first == second
    assert render_verdict(first) == render_verdict(second)


def test_level_is_restricted_to_the_known_values():
    levels = {
        _verdict({1: 7.2}).level,
        _verdict({1: 6.0, 3: 7.2}).level,
        _verdict({1: 6.0, 3: 6.0, 6: 6.0}).level,
    }

    assert levels <= set(RISK_LEVELS)
    assert "normal" in RISK_LEVELS


def test_invalid_arguments_are_rejected():
    with pytest.raises(ValueError, match="threshold"):
        _verdict({1: 7.2}, threshold=float("nan"))
    with pytest.raises(ValueError, match="predictions"):
        _verdict({})
    with pytest.raises(ValueError, match="horizon"):
        _verdict({0: 7.2})
    with pytest.raises(ValueError, match="anchor_at"):
        _verdict({1: 7.2}, last_observed_at=ANCHOR + timedelta(hours=1))

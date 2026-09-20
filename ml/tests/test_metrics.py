"""Contract tests for hydrological forecast metrics."""

import numpy as np
import pandas as pd
import pytest

from river_sentinel_ml.metrics import ForecastMetrics, evaluate_forecasts


def _make_frame(observed: list[float], predictions: dict[int, list[float]]) -> pd.DataFrame:
    index = pd.date_range("2024-06-01T00:00:00Z", periods=len(observed), freq="h")
    frame = pd.DataFrame({"observed": observed}, index=index, dtype="float64")
    for horizon, values in predictions.items():
        frame[f"prediction_h{horizon}"] = pd.Series(values, index=index, dtype="float64")
    return frame


def test_hand_computed_metrics_for_horizon_one():
    # o = [1, 2, 3, 4, 5]，p = [1.5, 2.5, 2.5, 4.5, 4.0]
    # 残差 = [0.5, 0.5, -0.5, 0.5, -1.0]
    # MAE = (0.5+0.5+0.5+0.5+1.0)/5 = 0.6
    # SS_res = 0.25+0.25+0.25+0.25+1.0 = 2.0，RMSE = sqrt(2.0/5) ≈ 0.6324555320336759
    # 观测均值 = 3.0，SS_tot = 4+1+0+1+4 = 10.0，R² = NSE = 1 - 2.0/10.0 = 0.8
    # 峰值误差 = |max(o) - max(p)| = |5.0 - 4.5| = 0.5
    frame = _make_frame([1.0, 2.0, 3.0, 4.0, 5.0], {1: [1.5, 2.5, 2.5, 4.5, 4.0]})

    metrics = evaluate_forecasts(frame, horizons=(1,))[1]

    assert metrics.sample_count == 5
    assert metrics.mae == pytest.approx(0.6)
    assert metrics.rmse == pytest.approx(0.6324555320336759)
    assert metrics.r2 == pytest.approx(0.8)
    assert metrics.nse == pytest.approx(0.8)
    assert metrics.peak_absolute_error == pytest.approx(0.5)


def test_zero_target_variance_returns_none_for_r2_and_nse():
    # o = [2, 2, 2] → SS_tot = 0，R² 与 NSE 无定义，必须返回 None
    # p = [2.0, 2.5, 2.0] → 残差 = [0.0, 0.5, 0.0]
    # MAE = 0.5/3 ≈ 0.16666666666666666，RMSE = sqrt(0.25/3) ≈ 0.28867513459481287
    # 峰值误差 = |2.0 - 2.5| = 0.5
    frame = _make_frame([2.0, 2.0, 2.0], {1: [2.0, 2.5, 2.0]})

    metrics = evaluate_forecasts(frame, horizons=(1,))[1]

    assert metrics.sample_count == 3
    assert metrics.r2 is None
    assert metrics.nse is None
    assert metrics.mae == pytest.approx(0.16666666666666666)
    assert metrics.rmse == pytest.approx(0.28867513459481287)
    assert metrics.peak_absolute_error == pytest.approx(0.5)


def test_peak_absolute_error_compares_maxima_over_valid_pairs():
    # o = [1, 2, 3]，p = [1.0, 5.0, 3.0] → 峰值误差 = |3.0 - 5.0| = 2.0
    # 残差 = [0.0, 3.0, 0.0] → MAE = 1.0
    frame = _make_frame([1.0, 2.0, 3.0], {1: [1.0, 5.0, 3.0]})

    metrics = evaluate_forecasts(frame, horizons=(1,))[1]

    assert metrics.sample_count == 3
    assert metrics.peak_absolute_error == pytest.approx(2.0)
    assert metrics.mae == pytest.approx(1.0)


def test_pairwise_missing_predictions_are_dropped():
    # o = [1, 2, 3, 4]，p = [NaN, 1.0, 2.0, NaN] → 有效对 (2.0, 1.0) 与 (3.0, 2.0)
    # 残差 = [-1.0, -1.0] → MAE = 1.0，RMSE = 1.0，SS_res = 2.0
    # 观测均值 = 2.5，SS_tot = 0.25+0.25 = 0.5 → R² = NSE = 1 - 2.0/0.5 = -3.0
    # 峰值误差 = |3.0 - 2.0| = 1.0
    frame = _make_frame([1.0, 2.0, 3.0, 4.0], {1: [np.nan, 1.0, 2.0, np.nan]})

    metrics = evaluate_forecasts(frame, horizons=(1,))[1]

    assert metrics.sample_count == 2
    assert metrics.mae == pytest.approx(1.0)
    assert metrics.rmse == pytest.approx(1.0)
    assert metrics.r2 == pytest.approx(-3.0)
    assert metrics.nse == pytest.approx(-3.0)
    assert metrics.peak_absolute_error == pytest.approx(1.0)


def test_pairwise_missing_observations_are_dropped():
    # o = [1.0, NaN, 3.0]，p = [1.5, 2.0, 3.5] → 有效对 (1.0, 1.5) 与 (3.0, 3.5)
    # 残差 = [0.5, 0.5] → MAE = RMSE = 0.5，SS_res = 0.5
    # 观测均值 = 2.0，SS_tot = 1.0+1.0 = 2.0 → R² = NSE = 1 - 0.5/2.0 = 0.75
    # 峰值误差 = |3.0 - 3.5| = 0.5
    frame = _make_frame([1.0, np.nan, 3.0], {1: [1.5, 2.0, 3.5]})

    metrics = evaluate_forecasts(frame, horizons=(1,))[1]

    assert metrics.sample_count == 2
    assert metrics.mae == pytest.approx(0.5)
    assert metrics.rmse == pytest.approx(0.5)
    assert metrics.r2 == pytest.approx(0.75)
    assert metrics.nse == pytest.approx(0.75)
    assert metrics.peak_absolute_error == pytest.approx(0.5)


def test_default_horizons_cover_one_three_and_six():
    frame = _make_frame(
        [1.0, 1.2, 1.5, 1.4, 1.6, 1.3, 1.1, 1.0],
        {
            1: [np.nan, 1.0, 1.2, 1.5, 1.4, 1.6, 1.3, 1.1],
            3: [np.nan, np.nan, np.nan, 1.0, 1.2, 1.5, 1.4, 1.6],
            6: [np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, 1.0, 1.2],
        },
    )

    result = evaluate_forecasts(frame)

    assert set(result) == {1, 3, 6}
    assert result[1].sample_count == 7
    assert result[3].sample_count == 5
    assert result[6].sample_count == 2


def test_unknown_horizon_column_is_rejected():
    frame = _make_frame([1.0, 2.0], {1: [1.0, 2.0]})

    with pytest.raises(ValueError):
        evaluate_forecasts(frame, horizons=(1, 3))


def test_horizon_without_valid_pairs_is_rejected():
    frame = _make_frame([1.0, 2.0, 3.0], {1: [np.nan, np.nan, np.nan]})

    with pytest.raises(ValueError):
        evaluate_forecasts(frame, horizons=(1,))


def test_missing_observed_column_is_rejected():
    frame = _make_frame([1.0, 2.0], {1: [1.0, 2.0]}).drop(columns=["observed"])

    with pytest.raises(ValueError):
        evaluate_forecasts(frame, horizons=(1,))


def test_empty_horizons_returns_empty_mapping():
    frame = _make_frame([1.0, 2.0], {1: [1.0, 2.0]})

    assert evaluate_forecasts(frame, horizons=()) == {}


def test_metrics_fields_are_python_scalars():
    frame = _make_frame([1.0, 2.0, 3.0, 4.0, 5.0], {1: [1.5, 2.5, 2.5, 4.5, 4.0]})

    metrics = evaluate_forecasts(frame, horizons=(1,))[1]

    assert isinstance(metrics, ForecastMetrics)
    assert type(metrics.sample_count) is int
    assert type(metrics.mae) is float
    assert type(metrics.rmse) is float
    assert type(metrics.r2) is float
    assert type(metrics.nse) is float
    assert type(metrics.peak_absolute_error) is float

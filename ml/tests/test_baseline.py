"""Contract tests for the causal persistence baseline."""

import numpy as np
import pandas as pd
import pytest

from river_sentinel_ml.baseline import persistence_forecast

DEFAULT_COLUMNS = {"observed", "prediction_h1", "prediction_h3", "prediction_h6"}
SAMPLE = [1.0, 1.2, 1.5, 1.4]


def _make_series(values: list[float], start: str = "2024-06-01T00:00:00Z") -> pd.Series:
    index = pd.date_range(start, periods=len(values), freq="h")
    return pd.Series(values, index=index, name="water_level_m", dtype="float64")


def test_horizon_one_prediction_equals_previous_observation():
    result = persistence_forecast(_make_series(SAMPLE))

    assert result["prediction_h1"].iloc[1] == pytest.approx(1.0)
    assert np.isnan(result["prediction_h1"].iloc[0])


def test_horizon_three_prediction_equals_observation_three_steps_earlier():
    result = persistence_forecast(_make_series(SAMPLE))

    assert result["prediction_h3"].iloc[3] == pytest.approx(1.0)
    for position in (0, 1, 2):
        assert np.isnan(result["prediction_h3"].iloc[position])


def test_default_columns_and_index_are_preserved():
    series = _make_series(SAMPLE)
    result = persistence_forecast(series)

    assert set(result.columns) == DEFAULT_COLUMNS
    assert list(result.columns) == [
        "observed",
        "prediction_h1",
        "prediction_h3",
        "prediction_h6",
    ]
    pd.testing.assert_index_equal(result.index, series.index)


def test_leading_rows_without_history_are_nan():
    result = persistence_forecast(_make_series(SAMPLE))

    assert np.isnan(result["prediction_h1"].iloc[0])
    assert result["prediction_h3"].iloc[:3].isna().all()
    assert result["prediction_h6"].isna().all()


def test_tail_rows_keep_observed_value_and_causal_prediction():
    result = persistence_forecast(_make_series(SAMPLE))

    assert result["observed"].tolist() == SAMPLE
    assert result["observed"].notna().all()
    assert result["prediction_h1"].iloc[-1] == pytest.approx(1.5)
    assert result["prediction_h3"].iloc[-1] == pytest.approx(1.0)
    assert np.isnan(result["prediction_h6"].iloc[-1])


def test_custom_horizons_produce_matching_columns():
    result = persistence_forecast(_make_series(SAMPLE), horizons=(1, 2))

    assert set(result.columns) == {"observed", "prediction_h1", "prediction_h2"}
    assert result["prediction_h2"].iloc[2] == pytest.approx(1.0)
    assert np.isnan(result["prediction_h2"].iloc[1])


def test_predictions_are_shifted_observations_for_every_row():
    values = [1.0, 1.4, 1.1, 2.3, 2.0, 1.8, 1.9]
    result = persistence_forecast(_make_series(values), horizons=(1, 2))
    observed = result["observed"].to_numpy()

    for horizon in (1, 2):
        column = result[f"prediction_h{horizon}"].to_numpy()
        assert np.isnan(column[:horizon]).all()
        for row in range(horizon, len(values)):
            assert column[row] == pytest.approx(observed[row - horizon])


@pytest.mark.parametrize("horizons", [[0], [-1], [1, 0], [1.5], ["3"], [True], [None]])
def test_invalid_horizons_are_rejected(horizons):
    with pytest.raises(ValueError):
        persistence_forecast(_make_series(SAMPLE), horizons=horizons)


def test_non_unique_index_is_rejected():
    index = pd.to_datetime(["2024-06-01T00:00:00Z", "2024-06-01T01:00:00Z", "2024-06-01T01:00:00Z"])
    series = pd.Series([1.0, 1.2, 1.5], index=index, dtype="float64")

    with pytest.raises(ValueError):
        persistence_forecast(series)


def test_non_monotonic_index_is_rejected():
    index = pd.to_datetime(["2024-06-01T00:00:00Z", "2024-06-01T02:00:00Z", "2024-06-01T01:00:00Z"])
    series = pd.Series([1.0, 1.2, 1.5], index=index, dtype="float64")

    with pytest.raises(ValueError):
        persistence_forecast(series)

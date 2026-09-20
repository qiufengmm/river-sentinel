"""Tests for the causal hourly sequence builder and the chronological split.

The suite fixes the Task 6 contract before any implementation exists: every
assertion below describes observable behaviour, not internals, so the module can
only satisfy them with a causal resampling and an order-preserving split.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd
import pytest

from river_sentinel_ml.dataset import DatasetSplit, chronological_split, resample_water_level

TIMEZONE = "Asia/Shanghai"
INPUT_COLUMNS: list[str] = [
    "source_record_id",
    "observed_at",
    "water_level_m",
    "station_code",
    "station_name",
    "quality_flag",
]
OUTPUT_COLUMNS: list[str] = [
    "source_record_id",
    "observed_at",
    "water_level_m",
    "station_code",
    "station_name",
    "quality_flag",
    "is_imputed",
]
NORMAL_FLAG = "normal"
IMPUTED_FLAG = "imputed"
STATION_CODE = "ST001"
STATION_NAME = "文成站"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _at(value: str) -> pd.Timestamp:
    """Build one ``Asia/Shanghai`` aware timestamp from a wall-clock string."""
    return pd.Timestamp(value, tz=TIMEZONE)


def _build_frame(
    record_ids: Sequence[str],
    stamps: Sequence[pd.Timestamp],
    levels: Sequence[float],
) -> pd.DataFrame:
    """Build a Task-5 shaped frame with exactly the upstream contract dtypes."""
    size = len(levels)
    return pd.DataFrame(
        {
            "source_record_id": pd.Series(list(record_ids), dtype="object"),
            "observed_at": pd.Series(list(stamps), dtype=f"datetime64[ns, {TIMEZONE}]"),
            "water_level_m": pd.Series(list(levels), dtype="float64"),
            "station_code": pd.Series([STATION_CODE] * size, dtype="object"),
            "station_name": pd.Series([STATION_NAME] * size, dtype="object"),
            "quality_flag": pd.Series([NORMAL_FLAG] * size, dtype="object"),
        }
    )


def _hourly_frame(levels: dict[int, float]) -> pd.DataFrame:
    """Build an hourly frame keyed by the hour of ``2026-01-01``."""
    hours = sorted(levels)
    return _build_frame(
        [f"r{hour:02d}" for hour in hours],
        [_at(f"2026-01-01 {hour:02d}:00:00") for hour in hours],
        [levels[hour] for hour in hours],
    )


def _quarterly_frame(hours: Iterable[int]) -> pd.DataFrame:
    """Build a 15-minute frame whose level encodes its own wall clock."""
    record_ids: list[str] = []
    stamps: list[pd.Timestamp] = []
    levels: list[float] = []
    for hour in hours:
        for minute in (0, 15, 30, 45):
            record_ids.append(f"r{hour:02d}{minute:02d}")
            stamps.append(_at(f"2026-01-01 {hour:02d}:{minute:02d}:00"))
            levels.append(round(hour + minute / 100.0, 4))
    return _build_frame(record_ids, stamps, levels)


def _gapped_frame() -> pd.DataFrame:
    """Build an hourly frame with one three-step and one four-step gap.

    Present hours are 0, 1, 5, 6, 11 and 12, so the missing hours split into a
    three-step gap (02:00-04:00) and a four-step gap (07:00-10:00).
    """
    return _hourly_frame({0: 3.00, 1: 3.10, 5: 3.50, 6: 3.60, 11: 4.10, 12: 4.20})


def _by_hour(frame: pd.DataFrame) -> pd.DataFrame:
    """Index a same-day frame by its hour so assertions stay readable."""
    return frame.assign(hour=frame["observed_at"].dt.hour).set_index("hour")


def _sequence_frame(count: int) -> pd.DataFrame:
    """Build a strictly increasing hourly sequence of ``count`` observations."""
    stamps = pd.date_range(start=_at("2026-01-01 00:00:00"), periods=count, freq="1h")
    return _build_frame(
        [f"s{index:03d}" for index in range(count)],
        list(stamps),
        [round(3.0 + index * 0.01, 4) for index in range(count)],
    )


def _assert_tz_aware(frame: pd.DataFrame) -> None:
    assert isinstance(frame["observed_at"].dtype, pd.DatetimeTZDtype)
    assert str(frame["observed_at"].dtype.tz) == TIMEZONE


# --------------------------------------------------------------------------- #
# Resampling: aggregation causality
# --------------------------------------------------------------------------- #


def test_hourly_bucket_keeps_the_last_observation_of_the_hour() -> None:
    frame = _quarterly_frame(range(5))
    result = resample_water_level(frame, "1h")

    assert list(result["observed_at"]) == [_at(f"2026-01-01 {hour:02d}:00:00") for hour in range(5)]
    levels = result["water_level_m"].tolist()
    # 03:00 reports the 03:45 reading, never the first reading nor their mean.
    assert levels[3] == pytest.approx(3.45)
    assert levels[3] != pytest.approx(3.00)
    assert levels[3] != pytest.approx(3.225)
    assert not result["is_imputed"].any()


def test_resampling_does_not_depend_on_row_order() -> None:
    frame = _hourly_frame({2: 3.2, 0: 3.0, 1: 3.1})
    result = resample_water_level(frame, "1h")

    assert result["observed_at"].tolist() == [
        _at("2026-01-01 00:00:00"),
        _at("2026-01-01 01:00:00"),
        _at("2026-01-01 02:00:00"),
    ]
    assert result["water_level_m"].tolist() == pytest.approx([3.0, 3.1, 3.2])


def test_thirty_minute_frequency_is_supported() -> None:
    frame = _quarterly_frame(range(2))
    result = resample_water_level(frame, "30min")

    expected = pd.date_range(start=_at("2026-01-01 00:00:00"), periods=4, freq="30min", tz=TIMEZONE)
    assert result["observed_at"].tolist() == list(expected)
    assert result["water_level_m"].tolist() == pytest.approx([0.15, 0.45, 1.15, 1.45])


# --------------------------------------------------------------------------- #
# Resampling: gap handling
# --------------------------------------------------------------------------- #


def test_short_gaps_are_interpolated_and_flagged() -> None:
    frame = _hourly_frame({0: 3.00, 1: 3.10, 3: 3.30, 6: 3.60})
    result = resample_water_level(frame, "1h")
    by_hour = _by_hour(result)

    # One-step gap at 02:00 bounded by 01:00 and 03:00.
    assert by_hour.loc[2, "water_level_m"] == pytest.approx(3.20)
    # Two-step gap at 04:00-05:00 bounded by 03:00 and 06:00.
    assert by_hour.loc[4, "water_level_m"] == pytest.approx(3.40)
    assert by_hour.loc[5, "water_level_m"] == pytest.approx(3.50)

    for hour in (2, 4, 5):
        assert bool(by_hour.loc[hour, "is_imputed"]) is True
        assert by_hour.loc[hour, "quality_flag"] == IMPUTED_FLAG

    for hour in (0, 1, 3, 6):
        assert bool(by_hour.loc[hour, "is_imputed"]) is False
        assert by_hour.loc[hour, "quality_flag"] == NORMAL_FLAG
        assert by_hour.loc[hour, "source_record_id"] == f"r{hour:02d}"


def test_long_gap_stays_missing_and_unflagged() -> None:
    frame = _hourly_frame({0: 3.00, 1: 3.10, 6: 3.60})
    result = resample_water_level(frame, "1h")
    by_hour = _by_hour(result)

    for hour in (2, 3, 4, 5):
        assert bool(np.isnan(by_hour.loc[hour, "water_level_m"]))
        assert bool(by_hour.loc[hour, "is_imputed"]) is False
        # Rows synthesized purely as grid placeholders carry no source fields.
        assert pd.isna(by_hour.loc[hour, "source_record_id"])
        assert pd.isna(by_hour.loc[hour, "quality_flag"])


def test_zero_threshold_disables_interpolation() -> None:
    result = resample_water_level(_gapped_frame(), "1h", 0)

    assert not result["is_imputed"].any()
    assert result["water_level_m"].isna().sum() == 7
    assert result["quality_flag"].tolist().count(IMPUTED_FLAG) == 0


def test_threshold_of_three_fills_the_three_step_gap_only() -> None:
    result = resample_water_level(_gapped_frame(), "1h", 3)
    by_hour = _by_hour(result)

    assert bool(by_hour.loc[2, "is_imputed"]) is True
    assert by_hour.loc[2, "water_level_m"] == pytest.approx(3.20)
    assert by_hour.loc[3, "water_level_m"] == pytest.approx(3.30)
    assert by_hour.loc[4, "water_level_m"] == pytest.approx(3.40)

    for hour in (7, 8, 9, 10):
        assert bool(np.isnan(by_hour.loc[hour, "water_level_m"]))
        assert bool(by_hour.loc[hour, "is_imputed"]) is False


def test_default_threshold_keeps_a_four_step_gap_missing() -> None:
    result = resample_water_level(_gapped_frame())
    by_hour = _by_hour(result)

    assert not result["is_imputed"].any()
    for hour in (2, 3, 4, 7, 8, 9, 10):
        assert bool(np.isnan(by_hour.loc[hour, "water_level_m"]))


# --------------------------------------------------------------------------- #
# Resampling: output contract
# --------------------------------------------------------------------------- #


def test_output_grid_is_continuous_and_complete() -> None:
    result = resample_water_level(_hourly_frame({0: 3.0, 6: 3.6}), "1h")

    differences = result["observed_at"].diff().dropna()
    assert (differences.dt.total_seconds() == 3600.0).all()
    assert result["observed_at"].tolist() == [
        _at(f"2026-01-01 {hour:02d}:00:00") for hour in range(7)
    ]
    assert result["water_level_m"].isna().sum() == 5


def test_output_frame_matches_the_column_and_dtype_contract() -> None:
    frame = _hourly_frame({0: 3.0, 1: 3.1, 2: 3.2})
    result = resample_water_level(frame, "1h")

    assert list(result.columns) == OUTPUT_COLUMNS
    assert result["is_imputed"].dtype == bool
    assert result["water_level_m"].dtype == "float64"
    assert result["source_record_id"].dtype == "object"
    _assert_tz_aware(result)
    pd.testing.assert_index_equal(
        result.index, pd.RangeIndex(start=0, stop=len(result), step=1), exact=True
    )


def test_empty_input_returns_a_typed_empty_frame() -> None:
    result = resample_water_level(_build_frame([], [], []))

    assert result.empty
    assert list(result.columns) == OUTPUT_COLUMNS
    assert result["is_imputed"].dtype == bool
    assert result["water_level_m"].dtype == "float64"
    _assert_tz_aware(result)


def test_input_frame_is_not_modified() -> None:
    frame = _hourly_frame({0: 3.0, 3: 3.3})
    before = frame.copy(deep=True)

    resample_water_level(frame, "1h")

    pd.testing.assert_frame_equal(frame, before)


# --------------------------------------------------------------------------- #
# Resampling: input validation
# --------------------------------------------------------------------------- #


def test_non_dataframe_input_is_rejected() -> None:
    with pytest.raises(TypeError):
        resample_water_level([{"observed_at": _at("2026-01-01 00:00:00"), "water_level_m": 3.0}])


@pytest.mark.parametrize("column", ["observed_at", "water_level_m"])
def test_missing_required_columns_are_rejected(column: str) -> None:
    frame = _hourly_frame({0: 3.0, 1: 3.1}).drop(columns=[column])

    with pytest.raises(ValueError, match=column):
        resample_water_level(frame, "1h")


def test_naive_timestamps_are_rejected() -> None:
    frame = _hourly_frame({0: 3.0, 1: 3.1})
    frame["observed_at"] = frame["observed_at"].dt.tz_localize(None)

    with pytest.raises(ValueError, match="timezone-aware"):
        resample_water_level(frame, "1h")


def test_non_datetime_timestamps_are_rejected() -> None:
    frame = _hourly_frame({0: 3.0, 1: 3.1})
    frame["observed_at"] = ["2026-01-01 00:00:00", "2026-01-01 01:00:00"]

    with pytest.raises(ValueError, match="observed_at"):
        resample_water_level(frame, "1h")


def test_non_numeric_levels_are_rejected() -> None:
    frame = _hourly_frame({0: 3.0, 1: 3.1})
    frame["water_level_m"] = ["3.00", "3.10"]

    with pytest.raises(ValueError, match="water_level_m"):
        resample_water_level(frame, "1h")


def test_missing_timestamps_are_rejected() -> None:
    frame = _hourly_frame({0: 3.0, 1: 3.1})
    frame.loc[0, "observed_at"] = pd.NaT

    with pytest.raises(ValueError, match="observed_at"):
        resample_water_level(frame, "1h")


@pytest.mark.parametrize("frequency", ["abc", "1 lightyear", "!!"])
def test_invalid_frequency_is_rejected(frequency: str) -> None:
    frame = _hourly_frame({0: 3.0, 1: 3.1})

    with pytest.raises(ValueError, match="frequency"):
        resample_water_level(frame, frequency)


def test_empty_frequency_is_rejected() -> None:
    frame = _hourly_frame({0: 3.0, 1: 3.1})

    with pytest.raises(ValueError, match="frequency"):
        resample_water_level(frame, "")


@pytest.mark.parametrize("steps", [-1, -3, 1.5, True, "2"])
def test_invalid_interpolation_steps_are_rejected(steps: object) -> None:
    frame = _hourly_frame({0: 3.0, 2: 3.2})

    with pytest.raises(ValueError, match="max_interpolation_steps"):
        resample_water_level(frame, "1h", steps)


# --------------------------------------------------------------------------- #
# Chronological split
# --------------------------------------------------------------------------- #


def test_each_partition_is_non_empty_and_time_sorted() -> None:
    frame = _sequence_frame(100)
    split = chronological_split(frame)

    assert isinstance(split, DatasetSplit)
    for part in (split.train, split.validation, split.test):
        assert not part.empty
        assert part["observed_at"].is_monotonic_increasing
        assert part["observed_at"].is_unique
        assert list(part.columns) == INPUT_COLUMNS
        pd.testing.assert_index_equal(
            part.index, pd.RangeIndex(start=0, stop=len(part), step=1), exact=True
        )


def test_partitions_are_strictly_ordered_in_time() -> None:
    split = chronological_split(_sequence_frame(100))

    assert split.train["observed_at"].max() < split.validation["observed_at"].min()
    assert split.validation["observed_at"].max() < split.test["observed_at"].min()


@pytest.mark.parametrize("count", [7, 20, 101])
def test_partitions_cover_every_timestamp_exactly_once(count: int) -> None:
    frame = _sequence_frame(count)
    split = chronological_split(frame)

    combined = pd.concat([split.train, split.validation, split.test], ignore_index=True)
    assert len(combined) == count
    assert combined["observed_at"].tolist() == frame["observed_at"].tolist()


@pytest.mark.parametrize("count", [7, 20, 101])
def test_partition_sizes_follow_the_ratios(count: int) -> None:
    split = chronological_split(_sequence_frame(count))

    expected_train = int(count * 0.70)
    expected_validation = int(count * 0.15)
    assert len(split.train) == expected_train
    assert len(split.validation) == expected_validation
    assert len(split.test) == count - expected_train - expected_validation
    assert abs(len(split.train) - count * 0.70) <= 1
    assert abs(len(split.validation) - count * 0.15) <= 1


def test_custom_ratios_are_honoured() -> None:
    split = chronological_split(_sequence_frame(40), train_ratio=0.5, validation_ratio=0.25)

    assert len(split.train) == 20
    assert len(split.validation) == 10
    assert len(split.test) == 10


@pytest.mark.parametrize("count", [7, 20, 101])
def test_split_is_deterministic(count: int) -> None:
    frame = _sequence_frame(count)
    first = chronological_split(frame)
    second = chronological_split(frame)

    pd.testing.assert_frame_equal(first.train, second.train)
    pd.testing.assert_frame_equal(first.validation, second.validation)
    pd.testing.assert_frame_equal(first.test, second.test)


def test_split_preserves_imputed_markers() -> None:
    levels = {hour: 3.0 + hour * 0.1 for hour in range(10) if hour != 2}
    frame = resample_water_level(_hourly_frame(levels), "1h")
    assert frame["is_imputed"].any()

    split = chronological_split(frame, train_ratio=0.3, validation_ratio=0.3)

    for part in (split.train, split.validation, split.test):
        assert "is_imputed" in part.columns
    assert split.train["is_imputed"].tolist() == [False, False, True]


# --------------------------------------------------------------------------- #
# Chronological split: input validation
# --------------------------------------------------------------------------- #


def test_duplicate_timestamps_are_rejected() -> None:
    frame = (
        pd.concat([_sequence_frame(10), _sequence_frame(2)], ignore_index=True)
        .sort_values("observed_at", kind="stable")
        .reset_index(drop=True)
    )
    assert frame["observed_at"].is_monotonic_increasing
    assert not frame["observed_at"].is_unique

    with pytest.raises(ValueError, match="duplicate"):
        chronological_split(frame)


def test_unsorted_timestamps_are_rejected() -> None:
    frame = _sequence_frame(10).sort_values("observed_at", ascending=False).reset_index(drop=True)

    with pytest.raises(ValueError, match="increasing"):
        chronological_split(frame)


@pytest.mark.parametrize("count", [0, 1, 6])
def test_too_few_rows_are_rejected(count: int) -> None:
    frame = _sequence_frame(count)

    with pytest.raises(ValueError, match="7"):
        chronological_split(frame)


def test_seven_rows_yield_non_empty_partitions() -> None:
    split = chronological_split(_sequence_frame(7))

    assert len(split.train) == 4
    assert len(split.validation) == 1
    assert len(split.test) == 2


def test_empty_partition_is_rejected() -> None:
    frame = _sequence_frame(7)

    with pytest.raises(ValueError, match="empty"):
        chronological_split(frame, train_ratio=0.9, validation_ratio=0.01)


def test_missing_timestamp_column_is_rejected() -> None:
    frame = _sequence_frame(10).drop(columns=["observed_at"])

    with pytest.raises(ValueError, match="observed_at"):
        chronological_split(frame)


def test_non_dataframe_split_input_is_rejected() -> None:
    with pytest.raises(TypeError):
        chronological_split([{"observed_at": _at("2026-01-01 00:00:00")}])


# --------------------------------------------------------------------------- #
# Downstream compatibility with Task 7
# --------------------------------------------------------------------------- #


def test_resampled_output_feeds_the_persistence_baseline() -> None:
    from river_sentinel_ml.baseline import persistence_forecast

    frame = _hourly_frame({hour: 3.0 + hour * 0.1 for hour in range(24)})
    result = resample_water_level(frame, "1h")

    series = pd.Series(
        result["water_level_m"].to_numpy(), index=pd.DatetimeIndex(result["observed_at"])
    )
    assert isinstance(series.index, pd.DatetimeIndex)
    assert series.dtype == "float64"

    forecast = persistence_forecast(series, horizons=(1, 2))
    # ``shift`` keeps the value observed two steps earlier, not the future value.
    assert forecast["prediction_h1"].iloc[1] == pytest.approx(3.0)
    assert forecast["prediction_h2"].iloc[2] == pytest.approx(3.0)


# --------------------------------------------------------------------------- #
# Regression guard: a ratio sum of 1 or more must fail loudly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("train_ratio", "validation_ratio"),
    [(0.80, 0.30), (0.99, 0.99), (0.70, 0.50)],
)
def test_ratio_sum_above_one_is_rejected(train_ratio: float, validation_ratio: float) -> None:
    frame = _sequence_frame(10)

    with pytest.raises(ValueError, match="empty"):
        chronological_split(frame, train_ratio=train_ratio, validation_ratio=validation_ratio)


@pytest.mark.parametrize(
    ("train_ratio", "validation_ratio"),
    [(0.70, 0.15), (0.50, 0.25), (0.80, 0.30), (0.99, 0.99), (0.34, 0.66), (0.90, 0.05)],
)
def test_no_partition_is_ever_empty_over_a_ratio_grid(
    train_ratio: float,
    validation_ratio: float,
) -> None:
    frame = _sequence_frame(40)

    try:
        split = chronological_split(
            frame, train_ratio=train_ratio, validation_ratio=validation_ratio
        )
    except ValueError:
        # Rounding may starve a partition; failing loudly is required because a
        # silently empty test set would reach the evaluation stage unnoticed.
        return

    sizes = {
        "train": len(split.train),
        "validation": len(split.validation),
        "test": len(split.test),
    }
    assert all(size > 0 for size in sizes.values()), sizes
    assert sum(sizes.values()) == len(frame)


# --------------------------------------------------------------------------- #
# Single-station guard: a resampled sequence may never mix stations
# --------------------------------------------------------------------------- #


def test_multiple_stations_are_rejected() -> None:
    frame = _hourly_frame({hour: 3.0 + hour * 0.1 for hour in range(24)})
    frame.loc[frame.index[:12], "station_code"] = "ST002"

    with pytest.raises(ValueError, match="station_code"):
        resample_water_level(frame, "1h")


def test_single_station_is_accepted() -> None:
    frame = _hourly_frame({hour: 3.0 + hour * 0.1 for hour in range(12)})

    result = resample_water_level(frame, "1h")

    assert len(result) == 12
    assert set(result["station_code"].dropna().unique()) == {STATION_CODE}


def test_missing_station_codes_do_not_count_as_stations() -> None:
    frame = _hourly_frame({hour: 3.0 + hour * 0.1 for hour in range(12)})
    frame["station_code"] = None

    result = resample_water_level(frame, "1h")

    assert len(result) == 12


def test_frames_without_a_station_code_column_are_accepted() -> None:
    frame = _hourly_frame({hour: 3.0 + hour * 0.1 for hour in range(12)})
    frame = frame.drop(columns=["station_code"])

    result = resample_water_level(frame, "1h")

    assert len(result) == 12
    assert bool(result["station_code"].isna().all())


def test_empty_frames_pass_the_single_station_guard() -> None:
    result = resample_water_level(_build_frame([], [], []), "1h")

    assert result.empty

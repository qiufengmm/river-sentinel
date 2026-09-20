"""Tests for sampling-interval inference."""

from __future__ import annotations

import pandas as pd
import pytest

from river_sentinel_ml.quality import infer_interval_minutes


def _index(values: list[str | None]) -> pd.DatetimeIndex:
    """Build a naive DatetimeIndex from ISO-like strings, tolerating ``None``."""
    return pd.DatetimeIndex(pd.to_datetime(values))


def test_hourly_series_returns_sixty_minutes() -> None:
    index = _index(
        [
            "2026-01-01 00:00:00",
            "2026-01-01 01:00:00",
            "2026-01-01 02:00:00",
            "2026-01-01 03:00:00",
        ]
    )
    assert infer_interval_minutes(index) == 60.0


def test_duplicate_times_keep_sixty_minutes() -> None:
    index = _index(
        [
            "2026-01-01 00:00:00",
            "2026-01-01 00:00:00",
            "2026-01-01 01:00:00",
            "2026-01-01 02:00:00",
            "2026-01-01 02:00:00",
        ]
    )
    assert infer_interval_minutes(index) == 60.0


def test_fewer_than_two_unique_times_returns_none() -> None:
    assert infer_interval_minutes(_index(["2026-01-01 00:00:00"])) is None
    assert infer_interval_minutes(_index(["2026-01-01 00:00:00", "2026-01-01 00:00:00"])) is None


def test_empty_index_returns_none() -> None:
    assert infer_interval_minutes(pd.DatetimeIndex([])) is None


def test_nat_values_are_dropped_before_inference() -> None:
    index = pd.DatetimeIndex(
        pd.to_datetime(["2026-01-01 00:00:00", None, "2026-01-01 01:00:00", None])
    )
    assert infer_interval_minutes(index) == 60.0


def test_unsorted_input_is_sorted_before_differencing() -> None:
    index = _index(
        [
            "2026-01-01 03:00:00",
            "2026-01-01 00:00:00",
            "2026-01-01 02:00:00",
            "2026-01-01 01:00:00",
        ]
    )
    assert infer_interval_minutes(index) == 60.0


def test_median_uses_positive_intervals_only() -> None:
    # 00:00 -> 01:00 -> 03:00 -> 04:00 gives 60, 120, 60 minutes.
    index = _index(
        [
            "2026-01-01 00:00:00",
            "2026-01-01 01:00:00",
            "2026-01-01 03:00:00",
            "2026-01-01 04:00:00",
        ]
    )
    assert infer_interval_minutes(index) == 60.0


def test_even_interval_count_averages_the_middle_pair() -> None:
    # 00:00 -> 01:00 -> 03:00 -> 04:00 -> 06:00 gives 60, 120, 60, 120 minutes.
    index = _index(
        [
            "2026-01-01 00:00:00",
            "2026-01-01 01:00:00",
            "2026-01-01 03:00:00",
            "2026-01-01 04:00:00",
            "2026-01-01 06:00:00",
        ]
    )
    assert infer_interval_minutes(index) == 90.0


def test_timezone_aware_index_is_supported() -> None:
    index = pd.DatetimeIndex(
        pd.to_datetime(
            [
                "2026-01-01 00:00:00",
                "2026-01-01 01:00:00",
                "2026-01-01 02:00:00",
            ]
        ).tz_localize("Asia/Shanghai")
    )
    assert infer_interval_minutes(index) == 60.0


def test_non_datetime_index_is_rejected() -> None:
    with pytest.raises(TypeError):
        infer_interval_minutes(pd.Index([1, 2, 3]))

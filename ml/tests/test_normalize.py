"""Tests for water-level row normalization and quality auditing."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from river_sentinel_ml.contracts import WaterLevelRecord
from river_sentinel_ml.ingestion.files import load_water_level_file
from river_sentinel_ml.normalize import normalize_water_level_rows

TIMEZONE = "Asia/Shanghai"
SAMPLE_PATH = Path(__file__).resolve().parents[2] / "data" / "samples" / "water_level_sample.csv"
OUTPUT_COLUMNS = {
    "source_record_id",
    "observed_at",
    "water_level_m",
    "station_code",
    "station_name",
    "quality_flag",
}


def _row(record_id: object, time_d: object, level: object, **extra: object) -> dict[str, object]:
    """Build one raw row laid out in the official field order."""
    row: dict[str, object] = {"z_id": record_id, "time_d": time_d, "up_water_level": level}
    row.update(extra)
    return row


def _at(value: str, timezone: str = TIMEZONE) -> pd.Timestamp:
    return pd.Timestamp(value, tz=timezone)


def _assert_conservation(summary: object) -> None:
    assert summary.total_rows == (
        summary.valid_rows
        + summary.invalid_timestamp_rows
        + summary.invalid_level_rows
        + summary.missing_level_rows
        + summary.duplicate_rows
    )


def test_valid_rows_are_normalized_to_the_contract_frame() -> None:
    rows = [
        _row(1001, "2026-01-01 00:00:00", "3.10"),
        _row(1002, "2026-01-01 01:00:00", "3.12"),
    ]
    frame, summary = normalize_water_level_rows(rows, TIMEZONE)

    assert set(frame.columns) == OUTPUT_COLUMNS
    assert isinstance(frame["observed_at"].dtype, pd.DatetimeTZDtype)
    assert frame["water_level_m"].dtype == "float64"
    assert frame["source_record_id"].tolist() == ["1001", "1002"]
    assert frame["water_level_m"].tolist() == pytest.approx([3.10, 3.12])
    assert frame["quality_flag"].tolist() == ["normal", "normal"]
    assert summary.total_rows == 2
    assert summary.valid_rows == 2
    _assert_conservation(summary)


def test_naive_timestamps_are_localized_to_the_configured_timezone() -> None:
    frame, _ = normalize_water_level_rows(
        [_row("1", "2026-06-01 12:00:00", "1.0")],
        TIMEZONE,
    )
    observed = frame.loc[0, "observed_at"]

    assert observed.tzinfo is not None
    assert observed == _at("2026-06-01 12:00:00")
    # Asia/Shanghai is UTC+08:00, so the same instant is 04:00 UTC.
    assert observed.tz_convert("UTC") == _at("2026-06-01 04:00:00", "UTC")


def test_aware_timestamps_are_converted_to_the_configured_timezone() -> None:
    frame, _ = normalize_water_level_rows(
        [_row("1", "2026-06-01T12:00:00+09:00", "1.0")],
        TIMEZONE,
    )
    assert frame.loc[0, "observed_at"] == _at("2026-06-01 11:00:00")


def test_nonexistent_local_time_is_rejected() -> None:
    # 2026-03-08 02:30 does not exist in America/New_York (spring forward).
    frame, summary = normalize_water_level_rows(
        [_row("1", "2026-03-08 02:30:00", "1.0")],
        "America/New_York",
    )
    assert frame.empty
    assert summary.invalid_timestamp_rows == 1
    assert summary.valid_rows == 0
    _assert_conservation(summary)


def test_ambiguous_local_time_is_rejected() -> None:
    # 2026-11-01 01:30 occurs twice in America/New_York (fall back).
    frame, summary = normalize_water_level_rows(
        [_row("1", "2026-11-01 01:30:00", "1.0")],
        "America/New_York",
    )
    assert frame.empty
    assert summary.invalid_timestamp_rows == 1
    assert summary.valid_rows == 0
    _assert_conservation(summary)


def test_duplicate_record_id_keeps_the_last_valid_occurrence() -> None:
    rows = [
        _row("A", "2026-01-01 00:00:00", "1.0"),
        _row("A", "2026-01-01 01:00:00", "2.0"),
    ]
    frame, summary = normalize_water_level_rows(rows, TIMEZONE)

    assert len(frame) == 1
    assert frame.loc[0, "water_level_m"] == pytest.approx(2.0)
    assert frame.loc[0, "observed_at"] == _at("2026-01-01 01:00:00")
    assert summary.duplicate_rows == 1
    assert summary.valid_rows == 1
    _assert_conservation(summary)


def test_invalid_timestamp_rows_are_excluded_and_counted() -> None:
    rows = [
        _row("1", "2026-01-01 25:00:00", "1.0"),
        _row("2", "not-a-timestamp", "2.0"),
        _row("3", None, "3.0"),
        _row("4", "2026-01-01 03:00:00", "4.0"),
    ]
    frame, summary = normalize_water_level_rows(rows, TIMEZONE)

    assert summary.invalid_timestamp_rows == 3
    assert summary.valid_rows == 1
    assert frame["source_record_id"].tolist() == ["4"]
    _assert_conservation(summary)


def test_non_numeric_level_rows_are_counted_as_invalid() -> None:
    rows = [
        _row("1", "2026-01-01 00:00:00", "abc"),
        _row("2", "2026-01-01 01:00:00", float("inf")),
        _row("3", "2026-01-01 02:00:00", "3.0"),
    ]
    frame, summary = normalize_water_level_rows(rows, TIMEZONE)

    assert summary.invalid_level_rows == 2
    assert summary.missing_level_rows == 0
    assert summary.valid_rows == 1
    assert frame["water_level_m"].tolist() == pytest.approx([3.0])
    _assert_conservation(summary)


def test_missing_level_rows_are_counted_and_never_zero_filled() -> None:
    rows = [
        _row("1", "2026-01-01 00:00:00", None),
        _row("2", "2026-01-01 01:00:00", ""),
        _row("3", "2026-01-01 02:00:00", "   "),
        _row("4", "2026-01-01 03:00:00", float("nan")),
        _row("5", "2026-01-01 04:00:00", "5.0"),
    ]
    frame, summary = normalize_water_level_rows(rows, TIMEZONE)

    assert summary.missing_level_rows == 4
    assert summary.invalid_level_rows == 0
    assert summary.valid_rows == 1
    assert frame["water_level_m"].tolist() == pytest.approx([5.0])
    assert 0.0 not in frame["water_level_m"].tolist()
    _assert_conservation(summary)


def test_empty_record_id_is_counted_as_invalid_timestamp() -> None:
    rows = [
        _row("", "2026-01-01 00:00:00", "1.0"),
        _row(None, "2026-01-01 01:00:00", "2.0"),
        _row("3", "2026-01-01 02:00:00", "3.0"),
    ]
    frame, summary = normalize_water_level_rows(rows, TIMEZONE)

    assert summary.invalid_timestamp_rows == 2
    assert summary.valid_rows == 1
    assert frame["source_record_id"].tolist() == ["3"]
    _assert_conservation(summary)


def test_station_fields_are_preserved_or_left_as_none() -> None:
    rows = [
        _row(
            "1",
            "2026-01-01 00:00:00",
            "1.0",
            station_code="330328001",
            station_name="文成站",
        ),
        _row("2", "2026-01-01 01:00:00", "2.0"),
    ]
    frame, _ = normalize_water_level_rows(rows, TIMEZONE)

    assert frame.loc[0, "station_code"] == "330328001"
    assert frame.loc[0, "station_name"] == "文成站"
    assert frame.loc[1, "station_code"] is None
    assert frame.loc[1, "station_name"] is None


def test_output_rows_satisfy_the_water_level_record_contract() -> None:
    rows = [
        _row("1", "2026-01-01 00:00:00", "1.0", station_code="330328001", station_name="文成站"),
        _row("2", "2026-01-01 01:00:00", 2),
        _row("3", "2026-01-01T02:00:00+08:00", "3.5"),
    ]
    frame, _ = normalize_water_level_rows(rows, TIMEZONE)

    for record in frame.to_dict(orient="records"):
        WaterLevelRecord(
            source_record_id=record["source_record_id"],
            observed_at=record["observed_at"],
            water_level_m=record["water_level_m"],
            station_code=record["station_code"],
            station_name=record["station_name"],
            quality_flag=record["quality_flag"],
        )


def test_output_is_sorted_by_time_then_record_id() -> None:
    rows = [
        _row("B", "2026-01-01 02:00:00", "1.0"),
        _row("A", "2026-01-01 02:00:00", "2.0"),
        _row("C", "2026-01-01 01:00:00", "3.0"),
    ]
    frame, _ = normalize_water_level_rows(rows, TIMEZONE)

    assert frame["source_record_id"].tolist() == ["C", "A", "B"]
    assert list(frame.index) == [0, 1, 2]


def test_conservation_holds_for_a_mixed_batch() -> None:
    rows = [
        _row("1", "2026-01-01 00:00:00", "1.0"),
        _row("2", "2026-01-01 01:00:00", "abc"),
        _row("3", "2026-01-01 02:00:00", None),
        _row("4", "2026-01-01 03:00:00", "4.0"),
        _row("4", "2026-01-01 04:00:00", "5.0"),
        _row("6", "2026-01-01 25:00:00", "6.0"),
    ]
    frame, summary = normalize_water_level_rows(rows, TIMEZONE)

    assert summary.total_rows == 6
    assert summary.valid_rows == len(frame) == 2
    assert summary.invalid_timestamp_rows == 1
    assert summary.invalid_level_rows == 1
    assert summary.missing_level_rows == 1
    assert summary.duplicate_rows == 1
    _assert_conservation(summary)


def test_empty_input_returns_a_typed_empty_frame() -> None:
    frame, summary = normalize_water_level_rows([], TIMEZONE)

    assert frame.empty
    assert set(frame.columns) == OUTPUT_COLUMNS
    assert isinstance(frame["observed_at"].dtype, pd.DatetimeTZDtype)
    assert frame["water_level_m"].dtype == "float64"
    assert summary.total_rows == 0
    assert summary.valid_rows == 0
    assert summary.start_at is None
    assert summary.end_at is None
    assert summary.inferred_interval_minutes is None


def test_iterable_input_is_accepted() -> None:
    rows = iter([_row("1", "2026-01-01 00:00:00", "1.0")])
    frame, summary = normalize_water_level_rows(rows, TIMEZONE)

    assert summary.total_rows == 1
    assert summary.valid_rows == 1
    assert frame["source_record_id"].tolist() == ["1"]


def test_sample_csv_normalizes_to_nine_valid_rows() -> None:
    rows = load_water_level_file(SAMPLE_PATH)
    frame, summary = normalize_water_level_rows(rows, TIMEZONE)

    assert summary.total_rows == 12
    assert summary.missing_level_rows == 1
    assert summary.invalid_timestamp_rows == 1
    assert summary.invalid_level_rows == 0
    assert summary.duplicate_rows == 1
    assert summary.valid_rows == 9
    assert len(frame) == 9
    _assert_conservation(summary)

    # The duplicated z_id 1005 keeps its last valid occurrence (05:00 / 3.35).
    duplicated = frame.loc[frame["source_record_id"] == "1005"].iloc[0]
    assert duplicated["observed_at"] == _at("2026-01-01 05:00:00")
    assert duplicated["water_level_m"] == pytest.approx(3.35)

    assert summary.inferred_interval_minutes == pytest.approx(60.0)
    assert summary.start_at == datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo(TIMEZONE))
    assert summary.end_at == datetime(2026, 1, 1, 11, 0, tzinfo=ZoneInfo(TIMEZONE))

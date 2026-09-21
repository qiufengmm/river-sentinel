"""Tests for the official ``cata_12720`` file adapter and the hourly grid builder.

The suite fixes the DS-2b contract with synthetic samples only: every fixture is
built inside ``tmp_path`` (GBK encoded, tiny, no real observation) so no credential,
absolute path or real row ever reaches Git. The single „禁止 dtype 猜测“ rule is
checked explicitly: every column that reaches the reader must be named by the caller.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from river_sentinel_ml.contracts import QualitySummary
from river_sentinel_ml.dataset import OUTPUT_COLUMNS as HOURLY_OUTPUT_COLUMNS
from river_sentinel_ml.ingestion.official_file import (
    DEFAULT_CSV_COLUMN_MAPPING,
    FileProvenance,
    build_hourly_water_level,
    describe_downloaded_file,
    inspect_official_archive,
    load_official_water_level_file,
    read_official_water_level_records,
)

TIMEZONE = "Asia/Shanghai"
HEADER = '"监测时间",实时水位,主键'
SOURCE_URI = (
    "https://data.wenzhou.gov.cn/jdop_front/resource/data/download.do?fileType=csv&iid=12720"
)
INPUT_COLUMNS: list[str] = [
    "source_record_id",
    "observed_at",
    "water_level_m",
    "station_code",
    "station_name",
    "quality_flag",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _write_csv(path: Path, lines: list[str], *, encoding: str = "gbk") -> Path:
    """Write a tiny CSV with official GBK headers."""
    payload = "\r\n".join(lines).encode(encoding) + b"\r\n"
    path.write_bytes(payload)
    return path


def _write_zip(path: Path, members: dict[str, list[str]], *, encoding: str = "gbk") -> Path:
    """Write a ZIP holding several official CSV shards."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, lines in members.items():
            archive.writestr(name, ("\r\n".join(lines) + "\r\n").encode(encoding))
    return path


def _row(moment: str, level: str, record_id: str) -> str:
    """Build one official data line."""
    return f"{moment},{level},{record_id}"


def _at(value: str) -> pd.Timestamp:
    """Build one ``Asia/Shanghai`` aware timestamp."""
    return pd.Timestamp(value, tz=TIMEZONE)


def _build_frame(
    record_ids: list[str],
    stamps: list[pd.Timestamp],
    levels: list[float],
    station_codes: list[str] | None = None,
) -> pd.DataFrame:
    """Build a Task-5 shaped frame with exactly the upstream contract dtypes."""
    size = len(levels)
    return pd.DataFrame(
        {
            "source_record_id": pd.Series(record_ids, dtype="object"),
            "observed_at": pd.Series(stamps, dtype=f"datetime64[ns, {TIMEZONE}]"),
            "water_level_m": pd.Series(levels, dtype="float64"),
            "station_code": pd.Series(
                station_codes if station_codes is not None else [None] * size, dtype="object"
            ),
            "station_name": pd.Series([None] * size, dtype="object"),
            "quality_flag": pd.Series(["normal"] * size, dtype="object"),
        }
    )


def _empty_frame(*, tz_aware: bool = True) -> pd.DataFrame:
    """Build a typed empty frame with or without timezone awareness."""
    dtype = f"datetime64[ns, {TIMEZONE}]" if tz_aware else "datetime64[ns]"
    return pd.DataFrame(
        {
            "source_record_id": pd.Series([], dtype="object"),
            "observed_at": pd.Series([], dtype=dtype),
            "water_level_m": pd.Series([], dtype="float64"),
            "station_code": pd.Series([], dtype="object"),
            "station_name": pd.Series([], dtype="object"),
            "quality_flag": pd.Series([], dtype="object"),
        }
    )


# --------------------------------------------------------------------------- #
# Archive reading
# --------------------------------------------------------------------------- #


def test_zip_shards_are_merged_and_deduplicated_keeping_the_last_copy(tmp_path: Path) -> None:
    """Shards may reorder their columns; the later copy of one key must win."""
    first = [
        HEADER,
        _row("2022-06-02 18:33:45.000", "7.04", "101"),
        _row("2022-06-02 18:34:15.000", "7.05", "102"),
    ]
    second = [HEADER, _row("2022-06-02 18:34:45.000", "7.06", "103")]
    # 分片边界重叠：第二个分片以乱序列顺序重复了主键 102 与 103。
    third = [
        "主键,实时水位,监测时间",
        "102,7.99,2022-06-02 18:34:15.000",
        "103,7.07,2022-06-02 18:34:45.000",
    ]
    archive = _write_zip(
        tmp_path / "official.zip",
        {"cata_12720_1.csv": first, "cata_12720_2.csv": second, "cata_12720_3.csv": third},
    )

    rows = read_official_water_level_records(archive)

    assert len(rows) == 3
    by_id = {str(row["z_id"]): row for row in rows}
    assert by_id["102"]["up_water_level"] == "7.99"
    assert by_id["103"]["up_water_level"] == "7.07"
    assert by_id["101"]["time_d"] == "2022-06-02 18:33:45.000"
    for row in rows:
        assert set(row) == set(DEFAULT_CSV_COLUMN_MAPPING.values())

    inspection = inspect_official_archive(archive)
    assert inspection.total_rows == 5
    assert inspection.unique_rows == 3
    assert inspection.duplicate_rows == 2
    assert inspection.rows_per_member == (
        ("cata_12720_1.csv", 2),
        ("cata_12720_2.csv", 1),
        ("cata_12720_3.csv", 2),
    )


def test_zip_without_a_csv_shard_is_rejected(tmp_path: Path) -> None:
    """A ZIP holding only non-CSV members merges to nothing: fail with the path."""
    archive = tmp_path / "notes.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("readme.txt", "no data here")

    with pytest.raises(ValueError, match="notes.zip"):
        read_official_water_level_records(archive)


def test_unsupported_suffix_is_rejected(tmp_path: Path) -> None:
    """XLS/XLSX needs an extra parser, so it must fail instead of being guessed."""
    workbook = tmp_path / "official.xlsx"
    workbook.write_bytes(b"\x00\x01binary")

    with pytest.raises(ValueError, match="official.xlsx") as caught:
        read_official_water_level_records(workbook)
    assert "xlsx" in str(caught.value).lower()


def test_undecodable_payload_is_rejected_instead_of_replaced(tmp_path: Path) -> None:
    """GBK bytes read as UTF-8 must raise, never silently substitute U+FFFD."""
    payload = _write_csv(
        tmp_path / "official.csv",
        [HEADER, _row("2022-06-02 18:33:45.000", "7.04", "101")],
    )

    with pytest.raises(ValueError) as caught:
        read_official_water_level_records(payload, encoding="utf-8")

    message = str(caught.value)
    assert "utf-8" in message
    assert "official.csv" in message
    # 必须能够用正确编码读回来：失败不是数据损坏。
    assert len(read_official_water_level_records(payload)) == 1


def test_missing_required_columns_are_rejected(tmp_path: Path) -> None:
    """Every missing official key must be named, including the localized target."""
    without_id = _write_csv(
        tmp_path / "no_id.csv", ["监测时间,实时水位", "2022-06-02 18:33:45.000,7.04"]
    )
    without_time = _write_csv(tmp_path / "no_time.csv", ["实时水位,主键", "7.04,101"])
    without_level = _write_csv(
        tmp_path / "no_level.csv", ["监测时间,主键", "2022-06-02 18:33:45.000,101"]
    )

    for path, missing in (
        (without_id, "z_id"),
        (without_time, "time_d"),
        (without_level, "up_water_level"),
    ):
        with pytest.raises(ValueError, match=missing):
            read_official_water_level_records(path)


def test_custom_column_mapping_is_honored(tmp_path: Path) -> None:
    """The mapping is caller-owned: no other column name is ever guessed."""
    payload = _write_csv(
        tmp_path / "renamed.csv",
        ["tm,z,pk", "2022-06-02 18:33:45.000,7.04,101"],
    )

    rows = read_official_water_level_records(
        payload, column_mapping={"tm": "time_d", "z": "up_water_level", "pk": "z_id"}
    )

    assert len(rows) == 1
    assert rows[0]["up_water_level"] == "7.04"


def test_missing_or_empty_file_is_rejected(tmp_path: Path) -> None:
    """A path that cannot be parsed must fail early, with the path in the message."""
    missing = tmp_path / "absent.csv"
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")

    for path in (missing, empty):
        with pytest.raises(ValueError, match=str(path).replace("\\", "\\\\")):
            read_official_water_level_records(path)


# --------------------------------------------------------------------------- #
# Normalization reuse
# --------------------------------------------------------------------------- #


def test_load_reuses_the_frozen_normalize_contract(tmp_path: Path) -> None:
    """The adapter must reuse Task 5 instead of reinventing cleaning."""
    payload = _write_csv(
        tmp_path / "official.csv",
        [
            HEADER,
            _row("2022-06-02 18:33:45.000", "7.04", "101"),
            _row("2022-06-02 18:34:15.000", "", "102"),  # 缺失水位
            _row("not-a-time", "7.06", "103"),  # 时间不可解析
            _row("2022-06-02 18:35:15.000", "not-a-number", "104"),  # 水位非法
        ],
    )

    frame, summary = load_official_water_level_file(payload, source_uri=SOURCE_URI)

    assert list(frame.columns) == INPUT_COLUMNS
    assert isinstance(summary, QualitySummary)
    assert str(frame["observed_at"].dtype) == f"datetime64[ns, {TIMEZONE}]"
    assert str(frame["water_level_m"].dtype) == "float64"
    assert frame["observed_at"].iloc[0].tz is not None
    assert (
        summary.total_rows
        == summary.valid_rows
        + summary.invalid_timestamp_rows
        + summary.invalid_level_rows
        + summary.missing_level_rows
        + summary.duplicate_rows
    )
    assert summary.total_rows == 4
    assert summary.missing_level_rows == 1
    assert summary.invalid_timestamp_rows == 1
    assert summary.invalid_level_rows == 1
    # 本适配器在读取阶段已按主键去重（保留末次），因此 Task 5 的 duplicate_rows
    # 在这里为 0；重复行数由 inspect_official_archive 单独给出证据。
    assert summary.duplicate_rows == 0
    assert summary.valid_rows == 1
    assert summary.inferred_interval_minutes is None


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


def test_describe_downloaded_file_matches_an_independent_digest(tmp_path: Path) -> None:
    """``sha256`` must equal an independently computed digest, lower-case hex."""
    payload = _write_csv(
        tmp_path / "official.csv",
        [HEADER, _row("2022-06-02 18:33:45.000", "7.04", "101")],
    )

    provenance = describe_downloaded_file(payload, source_uri=SOURCE_URI)

    assert isinstance(provenance, FileProvenance)
    assert provenance.sha256 == hashlib.sha256(payload.read_bytes()).hexdigest()
    assert provenance.sha256 == provenance.sha256.lower()
    assert len(provenance.sha256) == 64
    assert provenance.file_size_bytes == payload.stat().st_size
    assert provenance.record_count == 1
    assert provenance.file_format == "csv"
    assert provenance.dataset_id == "12720"
    assert provenance.acquired_at.endswith("Z")


def test_describe_rejects_a_missing_file(tmp_path: Path) -> None:
    """Provenance must never invent metadata for a file that is not there."""
    with pytest.raises(ValueError, match="does not exist"):
        describe_downloaded_file(tmp_path / "absent.csv", source_uri=SOURCE_URI)


# --------------------------------------------------------------------------- #
# Hourly grid
# --------------------------------------------------------------------------- #


def test_hourly_bucket_keeps_the_last_observation_of_the_hour(tmp_path: Path) -> None:
    """Bucket semantics are Task 6's: last reading inside the bucket, left edge label."""
    del tmp_path
    frame = _build_frame(
        ["r1", "r2", "r3", "r4"],
        [
            _at("2022-06-02 03:00:10"),
            _at("2022-06-02 03:15:00"),
            _at("2022-06-02 03:45:00"),
            _at("2022-06-02 04:00:00"),
        ],
        [1.0, 2.0, 4.5, 5.0],
    )

    hourly = build_hourly_water_level(frame, max_interpolation_steps=0)

    assert list(hourly.columns) == list(HOURLY_OUTPUT_COLUMNS)
    assert len(hourly) == 2
    assert hourly["observed_at"].iloc[0] == _at("2022-06-02 03:00:00")
    assert hourly["water_level_m"].iloc[0] == 4.5
    assert hourly["source_record_id"].iloc[0] == "r3"
    assert hourly["observed_at"].iloc[1] == _at("2022-06-02 04:00:00")


def test_readings_straddling_a_bucket_edge_stay_in_adjacent_buckets() -> None:
    """10:59:30 belongs to the 10:00 bucket, 11:00:30 to the 11:00 one."""
    frame = _build_frame(
        ["a", "b"],
        [_at("2022-06-02 10:59:30"), _at("2022-06-02 11:00:30")],
        [1.25, 2.75],
    )

    hourly = build_hourly_water_level(frame, max_interpolation_steps=0)

    assert len(hourly) == 2
    assert hourly["observed_at"].iloc[0] == _at("2022-06-02 10:00:00")
    assert hourly["water_level_m"].iloc[0] == 1.25
    assert hourly["observed_at"].iloc[1] == _at("2022-06-02 11:00:00")
    assert hourly["water_level_m"].iloc[1] == 2.75


def test_zero_steps_never_interpolates_and_two_steps_flags_the_fill() -> None:
    """``max_interpolation_steps`` is what decides whether a short gap is filled."""
    frame = _build_frame(
        ["a", "b", "c"],
        [_at("2022-06-02 01:00:00"), _at("2022-06-02 03:00:00"), _at("2022-06-02 05:00:00")],
        [1.0, 3.0, 5.0],
    )

    untouched = build_hourly_water_level(frame.copy(), max_interpolation_steps=0)
    assert untouched["water_level_m"].isna().sum() == 2
    assert untouched["is_imputed"].sum() == 0
    assert untouched["quality_flag"].tolist() == ["normal", None, "normal", None, "normal"]

    filled = build_hourly_water_level(frame.copy(), max_interpolation_steps=2)
    assert filled["water_level_m"].tolist() == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert filled["is_imputed"].tolist() == [False, True, False, True, False]
    assert filled.loc[1, "quality_flag"] == "imputed"
    assert filled.loc[1, "source_record_id"] is None


def test_max_interpolation_steps_is_required_and_validated() -> None:
    """No silent default: omitting, negating or fuzzing the threshold must fail."""
    frame = _build_frame(
        ["a", "b"],
        [_at("2022-06-02 01:00:00"), _at("2022-06-02 02:00:00")],
        [1.0, 2.0],
    )

    with pytest.raises(ValueError, match="max_interpolation_steps"):
        build_hourly_water_level(frame)
    for steps in (-1, 1.5, "2", True):
        with pytest.raises(ValueError, match="max_interpolation_steps"):
            build_hourly_water_level(frame.copy(), max_interpolation_steps=steps)


def test_naive_timestamps_are_rejected() -> None:
    """A timezone-less index would make the hourly grid ambiguous."""
    frame = pd.DataFrame(
        {
            "source_record_id": pd.Series(["a"], dtype="object"),
            "observed_at": pd.Series([pd.Timestamp("2022-06-02 01:00:00")], dtype="datetime64[ns]"),
            "water_level_m": pd.Series([1.0], dtype="float64"),
            "station_code": pd.Series([None], dtype="object"),
            "station_name": pd.Series([None], dtype="object"),
            "quality_flag": pd.Series(["normal"], dtype="object"),
        }
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        build_hourly_water_level(frame, max_interpolation_steps=0)


def test_several_stations_in_one_frame_are_rejected() -> None:
    """Mixing stations would interleave unrelated rivers into one series."""
    frame = _build_frame(
        ["a", "b"],
        [_at("2022-06-02 01:00:00"), _at("2022-06-02 02:00:00")],
        [1.0, 2.0],
        station_codes=["ST001", "ST002"],
    )

    with pytest.raises(ValueError, match="station_code"):
        build_hourly_water_level(frame, max_interpolation_steps=0)


def test_input_is_not_mutated_and_output_is_deterministic() -> None:
    """The helper is a pure function: same input, same output, input untouched."""
    frame = _build_frame(
        ["a", "b", "c"],
        [_at("2022-06-02 01:00:00"), _at("2022-06-02 02:30:00"), _at("2022-06-02 03:00:00")],
        [1.0, 2.0, 3.0],
    )
    before = frame.copy(deep=True)

    first = build_hourly_water_level(frame, max_interpolation_steps=1)
    second = build_hourly_water_level(frame, max_interpolation_steps=1)

    assert frame.equals(before)
    assert before.dtypes.to_dict() == frame.dtypes.to_dict()
    assert first.equals(second)


def test_empty_frame_returns_a_typed_empty_grid() -> None:
    """An empty but valid input must not lose its dtypes."""
    hourly = build_hourly_water_level(_empty_frame(), max_interpolation_steps=0)

    assert hourly.empty
    assert list(hourly.columns) == list(HOURLY_OUTPUT_COLUMNS)
    assert str(hourly["observed_at"].dtype) == f"datetime64[ns, {TIMEZONE}]"
    assert str(hourly["water_level_m"].dtype) == "float64"
    assert str(hourly["is_imputed"].dtype) == "bool"

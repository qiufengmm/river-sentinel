import json

import pandas as pd
import pytest

from river_sentinel_ml.ingestion.files import load_water_level_file


def test_load_csv_returns_official_fields(tmp_path) -> None:
    source = tmp_path / "water.csv"
    source.write_text(
        "time_d,up_water_level,z_id\n2026-01-01 00:00:00,3.1,1\n",
        encoding="utf-8",
    )

    rows = load_water_level_file(source)

    assert rows == [{"time_d": "2026-01-01 00:00:00", "up_water_level": 3.1, "z_id": 1}]


def test_load_json_returns_records(tmp_path) -> None:
    source = tmp_path / "water.json"
    source.write_text(
        json.dumps(
            [{"time_d": "2026-01-01 00:00:00", "up_water_level": 3.1, "z_id": "1"}]
        ),
        encoding="utf-8",
    )

    rows = load_water_level_file(source)

    assert rows[0]["z_id"] == "1"


def test_load_parquet_returns_records(tmp_path) -> None:
    source = tmp_path / "water.parquet"
    pd.DataFrame(
        [{"time_d": "2026-01-01 00:00:00", "up_water_level": 3.1, "z_id": "1"}]
    ).to_parquet(source, index=False)

    rows = load_water_level_file(source)

    assert rows[0]["up_water_level"] == 3.1


def test_unsupported_file_type_is_rejected(tmp_path) -> None:
    source = tmp_path / "water.xlsx"
    source.write_bytes(b"not-an-excel-file")

    with pytest.raises(ValueError, match="unsupported"):
        load_water_level_file(source)

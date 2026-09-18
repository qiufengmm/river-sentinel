from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from river_sentinel_ml.contracts import SourceManifest, WaterLevelRecord


def test_water_level_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        WaterLevelRecord(
            source_record_id="1",
            observed_at=datetime(2026, 1, 1, 8, 0, tzinfo=UTC).replace(tzinfo=None),
            water_level_m=3.2,
        )


def test_water_level_accepts_aware_timestamp() -> None:
    record = WaterLevelRecord(
        source_record_id="1",
        observed_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        water_level_m=3.2,
    )

    assert record.quality_flag == "normal"
    assert record.is_imputed is False


def test_source_manifest_rejects_invalid_checksum() -> None:
    with pytest.raises(ValidationError):
        SourceManifest(
            source_name="sample",
            source_uri="file:///data/sample.csv",
            dataset_id="12720",
            acquired_at=datetime(2026, 1, 1, tzinfo=UTC),
            request_parameters={},
            record_count=1,
            sha256="not-a-sha256",
        )

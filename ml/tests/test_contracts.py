from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from river_sentinel_ml.contracts import (
    ExperimentManifest,
    QualitySummary,
    SourceManifest,
    WaterLevelRecord,
)

BASE_TIME = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
SHA256 = "a" * 64


def _naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None)


def _quality_summary(**overrides: object) -> QualitySummary:
    payload: dict[str, object] = {
        "total_rows": 10,
        "valid_rows": 8,
        "duplicate_rows": 1,
        "invalid_timestamp_rows": 1,
        "invalid_level_rows": 0,
        "missing_level_rows": 0,
        "start_at": BASE_TIME,
        "end_at": BASE_TIME + timedelta(hours=9),
        "inferred_interval_minutes": 60.0,
    }
    payload.update(overrides)
    return QualitySummary(**payload)


def _experiment_manifest(**overrides: object) -> ExperimentManifest:
    payload: dict[str, object] = {
        "experiment_id": "baseline-2026-01",
        "created_at": BASE_TIME + timedelta(hours=12),
        "dataset_sha256": SHA256,
        "horizons": (1, 3, 6),
        "train_range": (BASE_TIME, BASE_TIME + timedelta(hours=3)),
        "validation_range": (BASE_TIME + timedelta(hours=4), BASE_TIME + timedelta(hours=6)),
        "test_range": (BASE_TIME + timedelta(hours=7), BASE_TIME + timedelta(hours=9)),
        "code_commit": "f21dc6e",
        "station_ids": ("feiyunjiang-01",),
        "feature_names": ("water_level_lag_1", "hour_of_day"),
        "model_name": "persistence",
        "parameters": {
            "horizon_steps": 6,
            "nested": {"enabled": True, "weights": [1, 2]},
            "note": None,
        },
        "random_seed": 20260918,
        "environment": {"python": "3.13.9", "platform": "win32"},
        "started_at": BASE_TIME,
        "finished_at": BASE_TIME + timedelta(hours=1),
        "artifact_paths": ("build/audit/baseline-metrics.json",),
        "status": "completed",
    }
    payload.update(overrides)
    return ExperimentManifest(**payload)


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


def test_water_level_rejects_empty_source_record_id() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        WaterLevelRecord(
            source_record_id="",
            observed_at=BASE_TIME,
            water_level_m=3.2,
        )


def test_water_level_rejects_nan_level() -> None:
    with pytest.raises(ValidationError, match="finite"):
        WaterLevelRecord(
            source_record_id="1",
            observed_at=BASE_TIME,
            water_level_m=float("nan"),
        )


def test_water_level_rejects_infinite_level() -> None:
    with pytest.raises(ValidationError, match="finite"):
        WaterLevelRecord(
            source_record_id="1",
            observed_at=BASE_TIME,
            water_level_m=float("inf"),
        )


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


def test_source_manifest_rejects_negative_record_count() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        SourceManifest(
            source_name="sample",
            source_uri="file:///data/sample.csv",
            dataset_id="12720",
            acquired_at=datetime(2026, 1, 1, tzinfo=UTC),
            request_parameters={"page": 1},
            record_count=-1,
            sha256=SHA256,
        )


def test_source_manifest_accepts_valid_payload() -> None:
    manifest = SourceManifest(
        source_name="温州市公共数据开放平台",
        source_uri="file:///data/samples/water_level_sample.csv",
        dataset_id="12720",
        acquired_at=datetime(2026, 1, 1, tzinfo=UTC),
        request_parameters={"page": 1, "page_size": 200},
        record_count=0,
        sha256=SHA256,
    )

    assert manifest.record_count == 0
    assert manifest.dataset_id == "12720"


def test_quality_summary_accepts_valid_summary() -> None:
    summary = _quality_summary()

    assert summary.valid_rows == 8
    assert summary.inferred_interval_minutes == 60.0


def test_quality_summary_rejects_valid_rows_above_total() -> None:
    with pytest.raises(ValidationError, match="valid_rows must not exceed total_rows"):
        _quality_summary(total_rows=5, valid_rows=8)


def test_quality_summary_rejects_reversed_time_range() -> None:
    with pytest.raises(ValidationError, match="start_at must not be later than end_at"):
        _quality_summary(end_at=BASE_TIME - timedelta(hours=1))


def test_quality_summary_rejects_naive_start_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _quality_summary(start_at=_naive(BASE_TIME))


def test_quality_summary_rejects_non_positive_interval() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        _quality_summary(inferred_interval_minutes=0.0)


def test_experiment_manifest_accepts_complete_manifest() -> None:
    manifest = _experiment_manifest()

    assert manifest.experiment_id == "baseline-2026-01"
    assert manifest.horizons == (1, 3, 6)
    assert manifest.station_ids == ("feiyunjiang-01",)
    assert manifest.random_seed == 20260918
    assert manifest.status == "completed"
    assert manifest.failure_reason is None


def test_experiment_manifest_rejects_empty_horizons() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        _experiment_manifest(horizons=())


def test_experiment_manifest_rejects_non_positive_horizon() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        _experiment_manifest(horizons=(1, 0, 6))


def test_experiment_manifest_rejects_empty_station_ids() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        _experiment_manifest(station_ids=())


def test_experiment_manifest_rejects_empty_feature_names() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        _experiment_manifest(feature_names=())


def test_experiment_manifest_rejects_reversed_data_range() -> None:
    with pytest.raises(ValidationError, match="must not be later than its end"):
        _experiment_manifest(train_range=(BASE_TIME + timedelta(hours=3), BASE_TIME))


def test_experiment_manifest_rejects_overlapping_partitions() -> None:
    with pytest.raises(ValidationError, match="must end before"):
        _experiment_manifest(
            validation_range=(BASE_TIME + timedelta(hours=3), BASE_TIME + timedelta(hours=6))
        )


def test_experiment_manifest_rejects_out_of_order_partitions() -> None:
    with pytest.raises(ValidationError, match="must end before"):
        _experiment_manifest(
            train_range=(BASE_TIME + timedelta(hours=7), BASE_TIME + timedelta(hours=9)),
            test_range=(BASE_TIME, BASE_TIME + timedelta(hours=3)),
        )


def test_experiment_manifest_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _experiment_manifest(created_at=_naive(BASE_TIME + timedelta(hours=12)))


def test_experiment_manifest_rejects_naive_started_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _experiment_manifest(started_at=_naive(BASE_TIME))


def test_experiment_manifest_rejects_naive_range_boundary() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _experiment_manifest(test_range=(BASE_TIME + timedelta(hours=7), _naive(BASE_TIME)))


def test_experiment_manifest_rejects_started_after_finished() -> None:
    with pytest.raises(ValidationError, match="started_at must not be later than finished_at"):
        _experiment_manifest(
            started_at=BASE_TIME + timedelta(hours=2),
            finished_at=BASE_TIME + timedelta(hours=1),
        )


def test_experiment_manifest_requires_failure_reason_when_failed() -> None:
    with pytest.raises(ValidationError, match="failure_reason is required"):
        _experiment_manifest(status="failed")


def test_experiment_manifest_rejects_failure_reason_when_completed() -> None:
    with pytest.raises(ValidationError, match="failure_reason must be empty"):
        _experiment_manifest(status="completed", failure_reason="unexpected error")


def test_experiment_manifest_accepts_failed_status_with_reason() -> None:
    manifest = _experiment_manifest(status="failed", failure_reason="insufficient rows")

    assert manifest.status == "failed"
    assert manifest.failure_reason == "insufficient rows"

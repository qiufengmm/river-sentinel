"""Validated data and experiment contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


def _require_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class WaterLevelRecord(BaseModel):
    """One normalized water-level observation."""

    source_record_id: str
    observed_at: datetime
    water_level_m: float
    station_code: str | None = None
    station_name: str | None = None
    is_imputed: bool = False
    quality_flag: Literal["normal", "suspected", "invalid", "imputed"] = "normal"

    _validate_observed_at = field_validator("observed_at")(_require_timezone_aware)


class SourceManifest(BaseModel):
    """Traceability metadata for one immutable source snapshot."""

    source_name: str
    source_uri: str = Field(min_length=1)
    dataset_id: str
    acquired_at: datetime
    request_parameters: dict[str, str | int]
    record_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    _validate_acquired_at = field_validator("acquired_at")(_require_timezone_aware)


class QualitySummary(BaseModel):
    """Counts and time coverage produced by normalization."""

    total_rows: int = Field(ge=0)
    valid_rows: int = Field(ge=0)
    duplicate_rows: int = Field(ge=0)
    invalid_timestamp_rows: int = Field(ge=0)
    invalid_level_rows: int = Field(ge=0)
    missing_level_rows: int = Field(ge=0)
    start_at: datetime | None
    end_at: datetime | None
    inferred_interval_minutes: float | None


class ExperimentManifest(BaseModel):
    """Minimum information required to reproduce a baseline experiment."""

    experiment_id: str
    created_at: datetime
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    horizons: tuple[int, ...]
    train_range: tuple[datetime, datetime]
    validation_range: tuple[datetime, datetime]
    test_range: tuple[datetime, datetime]
    code_commit: str

    _validate_created_at = field_validator("created_at")(_require_timezone_aware)

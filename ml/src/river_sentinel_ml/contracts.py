"""Validated data and experiment contracts."""

import math
from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field, field_validator, model_validator

# 顶层值限定为 JSON 标量；嵌套容器用 Any 表示，避免递归类型别名在
# pydantic 2.13 / Python 3.11 目标下触发 RecursionError。
JsonValue: TypeAlias = str | int | float | bool | None | list[Any] | dict[str, Any]


def _require_timezone_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


def _require_aware_range(value: tuple[datetime, datetime]) -> tuple[datetime, datetime]:
    _require_timezone_aware(value[0])
    _require_timezone_aware(value[1])
    return value


def _require_finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("water_level_m must be a finite number")
    return value


class WaterLevelRecord(BaseModel):
    """One normalized water-level observation."""

    source_record_id: str = Field(min_length=1)
    observed_at: datetime
    water_level_m: float
    station_code: str | None = None
    station_name: str | None = None
    is_imputed: bool = False
    quality_flag: Literal["normal", "suspected", "invalid", "imputed"] = "normal"

    _validate_observed_at = field_validator("observed_at")(_require_timezone_aware)
    _validate_water_level_m = field_validator("water_level_m")(_require_finite)


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

    @field_validator("start_at", "end_at")
    @classmethod
    def _validate_coverage_bounds(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        return _require_timezone_aware(value)

    @field_validator("inferred_interval_minutes")
    @classmethod
    def _validate_interval(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("inferred_interval_minutes must be greater than 0")
        return value

    @model_validator(mode="after")
    def _validate_counts_and_coverage(self) -> "QualitySummary":
        if self.valid_rows > self.total_rows:
            raise ValueError("valid_rows must not exceed total_rows")
        if self.start_at is not None and self.end_at is not None and self.start_at > self.end_at:
            raise ValueError("start_at must not be later than end_at")
        return self


class ExperimentManifest(BaseModel):
    """Minimum information required to reproduce a baseline experiment."""

    experiment_id: str = Field(min_length=1)
    created_at: datetime
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    horizons: tuple[int, ...] = Field(min_length=1)
    train_range: tuple[datetime, datetime]
    validation_range: tuple[datetime, datetime]
    test_range: tuple[datetime, datetime]
    code_commit: str
    station_ids: tuple[str, ...] = Field(min_length=1)
    feature_names: tuple[str, ...] = Field(min_length=1)
    model_name: str = Field(min_length=1)
    parameters: dict[str, JsonValue]
    random_seed: int
    environment: dict[str, str]
    started_at: datetime
    finished_at: datetime
    artifact_paths: tuple[str, ...]
    status: Literal["completed", "failed"]
    failure_reason: str | None = None

    _validate_created_at = field_validator("created_at")(_require_timezone_aware)
    _validate_started_at = field_validator("started_at")(_require_timezone_aware)
    _validate_finished_at = field_validator("finished_at")(_require_timezone_aware)
    _validate_train_range = field_validator("train_range")(_require_aware_range)
    _validate_validation_range = field_validator("validation_range")(_require_aware_range)
    _validate_test_range = field_validator("test_range")(_require_aware_range)

    @field_validator("horizons")
    @classmethod
    def _validate_horizons(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(horizon <= 0 for horizon in value):
            raise ValueError("horizons must all be greater than 0")
        return value

    @model_validator(mode="after")
    def _validate_ranges_and_status(self) -> "ExperimentManifest":
        for label, bounds in (
            ("train_range", self.train_range),
            ("validation_range", self.validation_range),
            ("test_range", self.test_range),
        ):
            start, end = bounds
            if start > end:
                raise ValueError(f"{label} start must not be later than its end")

        if self.started_at > self.finished_at:
            raise ValueError("started_at must not be later than finished_at")

        if self.train_range[1] >= self.validation_range[0]:
            raise ValueError("train_range must end before validation_range starts")
        if self.validation_range[1] >= self.test_range[0]:
            raise ValueError("validation_range must end before test_range starts")

        reason = (self.failure_reason or "").strip()
        if self.status == "failed" and not reason:
            raise ValueError("failure_reason is required when status is failed")
        if self.status == "completed" and reason:
            raise ValueError("failure_reason must be empty when status is completed")

        return self

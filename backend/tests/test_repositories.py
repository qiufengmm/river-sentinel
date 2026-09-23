"""样例读取、排序、质量剔除与产物查询（DS-8A）。

覆盖点：样例读取/排序、非法站点与时间、产物缺失返回 ``None``。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.app.config import Settings
from backend.app.errors import DataUnavailableError, InvalidRequestError
from backend.app.repositories.artifact_repository import ArtifactRepository
from backend.app.repositories.sample_repository import SampleRepository
from backend.app.timeutils import ensure_aware

TIMEZONE = ZoneInfo("Asia/Shanghai")


def test_history_is_time_ascending_and_finite(
    settings: Settings, repository: SampleRepository
) -> None:
    points = repository.history(settings.station_id)
    assert len(points) == 10
    times = [point.observed_at for point in points]
    assert times == sorted(times)
    assert all(point.water_level_m == point.water_level_m for point in points)
    assert all(point.observed_at.tzinfo is not None for point in points)


def test_quality_notes_record_dropped_rows(repository: SampleRepository) -> None:
    notes = repository.quality_notes
    joined = "\n".join(notes)
    assert "剔除非法时间戳行：1" in joined
    assert "剔除缺失或非有限水位行：1" in joined
    assert "合成演示数据" in joined


def test_unknown_station_is_rejected(repository: SampleRepository) -> None:
    with pytest.raises(InvalidRequestError) as error:
        repository.history("unknown-station")
    assert error.value.code == "INVALID_REQUEST"


def test_inverted_time_range_is_rejected(repository: SampleRepository, settings: Settings) -> None:
    start = datetime(2026, 1, 1, 5, tzinfo=TIMEZONE)
    end = datetime(2026, 1, 1, 1, tzinfo=TIMEZONE)
    with pytest.raises(InvalidRequestError):
        repository.history(settings.station_id, start, end)


def test_range_filter_and_empty_result(repository: SampleRepository, settings: Settings) -> None:
    start = datetime(2026, 1, 1, 3, tzinfo=TIMEZONE)
    end = datetime(2026, 1, 1, 5, tzinfo=TIMEZONE)
    inside = repository.history(settings.station_id, start, end)
    assert [point.observed_at.hour for point in inside] == [3, 4, 5]

    future = datetime(2030, 1, 1, tzinfo=TIMEZONE)
    assert repository.history(settings.station_id, future, future + timedelta(hours=1)) == []


def test_missing_sample_file_reports_unavailable(tmp_path: Path) -> None:
    broken = Settings(sample_path=tmp_path / "missing.csv")
    with pytest.raises(DataUnavailableError) as error:
        SampleRepository(broken).history(broken.station_id)
    assert error.value.code == "DATA_UNAVAILABLE"


def test_level_series_is_unique_and_monotonic(repository: SampleRepository) -> None:
    series = repository.level_series(None)
    assert len(series) == 10
    assert series.index.is_monotonic_increasing
    assert series.index.is_unique


def test_last_observed_at_with_until(repository: SampleRepository) -> None:
    until = datetime(2026, 1, 1, 5, tzinfo=TIMEZONE)
    assert repository.last_observed_at(until) == datetime(2026, 1, 1, 5, tzinfo=TIMEZONE)
    assert repository.last_observed_at() == datetime(2026, 1, 1, 11, tzinfo=TIMEZONE)


def test_artifact_lookup_returns_none_without_directory(settings: Settings) -> None:
    repository = ArtifactRepository(settings)
    assert repository.artifact_dir is None
    assert repository.load_comparison() is None
    assert (
        repository.find_prediction("xgboost", 1, datetime(2026, 1, 1, 11, tzinfo=TIMEZONE)) is None
    )


def test_artifact_lookup_reads_configured_file(tmp_path: Path) -> None:
    configured = Settings(artifact_dir=tmp_path)
    payload = {
        "model": "xgboost",
        "model_version": "ds6-e1",
        "as_of": "2026-01-01T11:00:00+08:00",
        "anchor_at": "2026-01-01T11:00:00+08:00",
        "points": [
            {"horizon": 1, "target_at": "2026-01-01T12:00:00+08:00", "water_level_m": 3.71},
            {"horizon": 3, "target_at": "2026-01-01T14:00:00+08:00", "water_level_m": 3.52},
        ],
    }
    (tmp_path / "predictions").mkdir()
    (tmp_path / "predictions" / "xgboost_h1.json").write_text(json.dumps(payload), encoding="utf-8")
    repository = ArtifactRepository(configured)
    found = repository.find_prediction("xgboost", 1, datetime(2026, 1, 1, 11, tzinfo=TIMEZONE))
    assert found is not None
    assert found.model_version == "ds6-e1"
    assert found.points[0].water_level_m == 3.71
    assert (
        repository.find_prediction("xgboost", 6, datetime(2026, 1, 1, 11, tzinfo=TIMEZONE)) is None
    )
    assert (
        repository.find_prediction("xgboost", 1, datetime(2026, 1, 1, 10, tzinfo=TIMEZONE)) is None
    )


def test_ensure_aware_keeps_existing_timezone() -> None:
    stamp = datetime(2026, 1, 1, 11, tzinfo=TIMEZONE)
    assert ensure_aware(stamp, "Asia/Shanghai") == stamp
    naive = datetime(2026, 1, 1, 11)
    assert ensure_aware(naive, "Asia/Shanghai").tzinfo is not None


@pytest.fixture
def repository(settings: Settings) -> SampleRepository:
    return SampleRepository(settings)

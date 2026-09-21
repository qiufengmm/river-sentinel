"""Immutable raw snapshots and normalized hourly tables for Open-Meteo rainfall.

本模块只做两件事：

1. 把 Open-Meteo Historical Weather API 的**原始 JSON 响应**写入不可变快照与相邻清单；
2. 把规范化后的**原始小时级**结果写成 CSV 小时表（保留小时时间戳与 ``point_id``）。

口径警示（写入本模块即固化，后续任务不得省略）：

- Open-Meteo Historical Weather API **Best Match** 提供的是**网格化再分析降雨**（响应不声明
  具体构成模型，待确认），不是文成县地面雨量站实测值，不能作为地面雨量站观测的等价替代，
  **也不得**表述为「ERA5-Land 实测」或「ERA5 实测」；
- 多点区域平均只能作为「文成县区域降雨代理变量」，本轮**不做任何平均**；
- 小时表**每行一小时**，不含任何日聚合、滚动累计或滞后特征；
- 本数据仅用于教学科研与辅助分析，不替代水行政主管部门的正式监测、预警、调度或应急决策。

命名与骨架风格参照包内既有的 ``provenance.py``，实现独立于它：本模块面向
「点 × 时间块」的 JSON 响应快照，而非 NDJSON 行快照。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pandas as pd

SNAPSHOT_NAMESPACE: Final[str] = "open-meteo"
HOURLY_TABLE_NAMESPACE: Final[str] = "hourly"
DATASET_ID: Final[str] = "open-meteo-archive-hourly"

# 清单与快照共用这份参数白名单：本数据源不需要任何凭据，
# 但一旦将来误传敏感键，也会被静默丢弃，保证凭据不落盘。
SENSITIVE_PARAMETER_KEYS: Final[frozenset[str]] = frozenset(
    {"apikey", "api_key", "appsecret", "authorization", "password", "token"}
)

HOURLY_TABLE_COLUMNS: Final[tuple[str, ...]] = (
    "observed_at",
    "precipitation_mm",
    "rain_mm",
    "point_id",
    "longitude",
    "latitude",
)

_SHA256_PREFIX_LENGTH: Final[int] = 12


def acquired_now() -> datetime:
    """Single acquisition clock; monkeypatch this to pin snapshot paths in tests."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class RainfallSnapshotMetadata:
    """Everything needed to place and describe one raw-response snapshot."""

    source_name: str
    source_uri: str
    dataset_id: str
    point_id: str
    start_date: str
    end_date: str
    request_parameters: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RainfallSnapshotManifest:
    """Traceability metadata for one raw-response snapshot."""

    source_name: str
    source_uri: str
    dataset_id: str
    point_id: str
    acquired_at: datetime
    request_parameters: dict[str, str | int | float | bool | None]
    record_count: int
    sha256: str


def _json_safe(value: object) -> object:
    """Return a JSON-safe copy of ``value`` with non-finite floats replaced by ``None``.

    非有限浮点（``NaN``、``+Infinity``、``-Infinity``）不是 RFC 8259 合法 JSON，
    这里在**取值位置**替换为 ``null``；映射的键保持原样，容器类型保持不变。
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def canonical_json_bytes(payload: object) -> bytes:
    """Serialize a raw response deterministically so its SHA-256 is reproducible."""
    return json.dumps(
        _json_safe(payload), ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode("utf-8")


def _sanitize_parameters(
    parameters: Mapping[str, object],
) -> dict[str, str | int | float | bool | None]:
    """Drop credential-like keys and reject values that are not JSON scalars."""
    sanitized: dict[str, str | int | float | bool | None] = {}
    for key, value in parameters.items():
        if str(key).strip().lower() in SENSITIVE_PARAMETER_KEYS:
            continue
        if not isinstance(value, str | int | float | bool) and value is not None:
            raise ValueError(
                f"request parameter {key!r} must be a JSON scalar, got {type(value).__name__}"
            )
        sanitized[str(key)] = value
    return sanitized


def _manifest_dict(manifest: RainfallSnapshotManifest) -> dict[str, object]:
    return {
        "source_name": manifest.source_name,
        "source_uri": manifest.source_uri,
        "dataset_id": manifest.dataset_id,
        "point_id": manifest.point_id,
        "acquired_at": manifest.acquired_at.isoformat(),
        "request_parameters": manifest.request_parameters,
        "record_count": manifest.record_count,
        "sha256": manifest.sha256,
    }


def write_hourly_snapshot(
    payload: object,
    metadata: RainfallSnapshotMetadata,
    root: Path,
    *,
    record_count: int,
    acquired_at: datetime | None = None,
) -> tuple[Path, Path, RainfallSnapshotManifest]:
    """Write one raw response plus its manifest under ``root``.

    布局：``<root>/open-meteo/<point_id>/<start_date>_<end_date>/<UTC时间戳>-<sha256前12位>.json``
    与同名 ``.manifest.json``。快照与清单均排他创建，**绝不覆盖**既有文件。
    """
    timestamp = acquired_at if acquired_at is not None else acquired_now()
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("acquired_at must be timezone-aware")

    snapshot_bytes = canonical_json_bytes(payload)
    sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    directory = (
        Path(root)
        / SNAPSHOT_NAMESPACE
        / metadata.point_id
        / f"{metadata.start_date}_{metadata.end_date}"
    )
    stem = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{sha256[:_SHA256_PREFIX_LENGTH]}"
    snapshot_path = directory / f"{stem}.json"
    manifest_path = directory / f"{stem}.manifest.json"

    directory.mkdir(parents=True, exist_ok=True)
    if snapshot_path.exists() or manifest_path.exists():
        raise FileExistsError(f"refusing to overwrite existing snapshot: {snapshot_path}")

    # 清单先构造、先序列化：构造失败就不会留下无清单的孤儿快照。
    manifest = RainfallSnapshotManifest(
        source_name=metadata.source_name,
        source_uri=metadata.source_uri,
        dataset_id=metadata.dataset_id,
        point_id=metadata.point_id,
        acquired_at=timestamp,
        request_parameters=_sanitize_parameters(metadata.request_parameters),
        record_count=record_count,
        sha256=sha256,
    )
    manifest_payload = json.dumps(
        _manifest_dict(manifest), ensure_ascii=False, sort_keys=True, allow_nan=False
    )

    try:
        with snapshot_path.open("xb") as snapshot_handle:
            snapshot_handle.write(snapshot_bytes)
        with manifest_path.open("x", encoding="utf-8") as manifest_handle:
            manifest_handle.write(f"{manifest_payload}\n")
    except BaseException:
        # 维持“快照必带清单”的不变式：任何失败都回滚，绝不留下半成品或孤儿快照。
        manifest_path.unlink(missing_ok=True)
        snapshot_path.unlink(missing_ok=True)
        raise
    return snapshot_path, manifest_path, manifest


def _validate_hourly_frame(frame: pd.DataFrame) -> None:
    missing = [column for column in HOURLY_TABLE_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"hourly frame is missing required column(s): {', '.join(missing)}")
    observed = frame["observed_at"]
    if observed.empty:
        if not str(observed.dtype).startswith("datetime64[ns, "):
            raise ValueError("observed_at must be a timezone-aware datetime column")
        return
    if observed.dt.tz is None:
        raise ValueError("observed_at must be timezone-aware")


def write_hourly_table(
    frame: pd.DataFrame,
    point_id: str,
    start_date: str,
    end_date: str,
    root: Path,
) -> Path:
    """Write the normalized hourly frame to CSV without any aggregation.

    布局：``<root>/open-meteo/hourly/<point_id>/<start_date>_<end_date>.csv``。
    每行一小时，``observed_at`` 为 ISO 8601 且带 ``+08:00`` 偏移，``point_id`` 保留；
    写入是原子的（临时文件 + ``os.replace``），不产生半成品。
    """
    if not point_id:
        raise ValueError("point_id must not be empty")
    _validate_hourly_frame(frame)

    table = pd.DataFrame(
        {
            "observed_at": pd.Series(
                [value.isoformat() for value in frame["observed_at"]], dtype="object"
            ),
            "precipitation_mm": frame["precipitation_mm"].to_numpy(),
            "rain_mm": frame["rain_mm"].to_numpy(),
            "point_id": frame["point_id"].to_numpy(),
            "longitude": frame["longitude"].to_numpy(),
            "latitude": frame["latitude"].to_numpy(),
        }
    )
    path = (
        Path(root)
        / SNAPSHOT_NAMESPACE
        / HOURLY_TABLE_NAMESPACE
        / point_id
        / f"{start_date}_{end_date}.csv"
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, suffix=".tmp", delete=False
    )
    try:
        with handle:
            table.to_csv(handle, index=False, encoding="utf-8", lineterminator="\n")
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return path


def snapshot_directories(root: Path, point_ids: Sequence[str]) -> list[Path]:
    """Return the per-point snapshot directories that exist under ``root``."""
    return [
        path
        for path in (Path(root) / SNAPSHOT_NAMESPACE / point_id for point_id in point_ids)
        if path.is_dir()
    ]

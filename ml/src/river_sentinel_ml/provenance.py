"""Immutable raw snapshot writing with source traceability."""

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from river_sentinel_ml.contracts import SourceManifest

SOURCE_NAMESPACE: Final[str] = "wenzhou"
SENSITIVE_PARAMETER_KEYS: Final[frozenset[str]] = frozenset(
    {"appsecret", "token", "authorization", "password"}
)
_SHA256_PREFIX_LENGTH: Final[int] = 12


def _acquired_now() -> datetime:
    """Single acquisition clock; monkeypatch this to pin snapshot paths in tests."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class SnapshotMetadata:
    source_name: str
    source_uri: str
    dataset_id: str
    request_parameters: Mapping[str, object] = field(default_factory=dict)


def _replace_non_finite(value: object) -> object:
    """Recursively turn non-finite floats (NaN, ±Infinity) into None in value positions.

    只重写取值位置：映射的键保持原样，list 仍是 list、tuple 仍是 tuple，
    其余类型（int、str、bool、None）原样返回。
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {key: _replace_non_finite(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_replace_non_finite(item) for item in value)
    if isinstance(value, list):
        return [_replace_non_finite(item) for item in value]
    return value


def _serialize_rows(rows: Iterable[Mapping[str, object]]) -> tuple[bytes, int]:
    """Render rows as deterministic NDJSON bytes and return them with the row count.

    非有限浮点值（``NaN``、``+Infinity``、``-Infinity``）序列化为 ``null``，
    使输出始终是 RFC 8259 合法 JSON；``allow_nan=False`` 作为兜底保险，
    若有非有限值漏网则抛 ``ValueError`` 而不是写入非法 JSON。
    """
    lines: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise TypeError(f"row {index} must be a mapping, got {type(row).__name__}")
        safe_row = _replace_non_finite(dict(row))
        lines.append(json.dumps(safe_row, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return "".join(f"{line}\n" for line in lines).encode("utf-8"), len(lines)


def _sanitize_parameters(parameters: Mapping[str, object]) -> dict[str, str | int]:
    """Drop credential-like keys and reject anything that is not a string or integer."""
    sanitized: dict[str, str | int] = {}
    for key, value in parameters.items():
        if str(key).strip().lower() in SENSITIVE_PARAMETER_KEYS:
            continue
        if isinstance(value, bool) or not isinstance(value, str | int):
            # Task 4 契约第 7 条要求：请求参数取值非法时抛 ValueError
            # （非法行类型才用 TypeError）。此处分支内含类型判断，
            # 与 lint 规则 TRY004 的建议冲突，以接口契约为准。
            message = f"request parameter {key!r} must be a string or an integer"
            raise ValueError(message)  # noqa: TRY004
        sanitized[str(key)] = value
    return sanitized


def _snapshot_paths(
    root: Path,
    dataset_id: str,
    acquired_at: datetime,
    sha256: str,
) -> tuple[Path, Path]:
    directory = (
        root
        / SOURCE_NAMESPACE
        / dataset_id
        / f"{acquired_at.year:04d}"
        / f"{acquired_at.month:02d}"
        / f"{acquired_at.day:02d}"
    )
    stem = f"{acquired_at.strftime('%Y%m%dT%H%M%SZ')}-{sha256[:_SHA256_PREFIX_LENGTH]}"
    return directory / f"{stem}.ndjson", directory / f"{stem}.manifest.json"


def write_raw_snapshot(
    rows: Iterable[Mapping[str, object]],
    metadata: SnapshotMetadata,
    root: Path,
) -> tuple[Path, Path, SourceManifest]:
    """写入不可变快照与相邻清单，返回（快照路径, 清单路径, 清单对象）。"""
    payload, record_count = _serialize_rows(rows)
    sha256 = hashlib.sha256(payload).hexdigest()
    acquired_at = _acquired_now()
    manifest = SourceManifest(
        source_name=metadata.source_name,
        source_uri=metadata.source_uri,
        dataset_id=metadata.dataset_id,
        acquired_at=acquired_at,
        request_parameters=_sanitize_parameters(metadata.request_parameters),
        record_count=record_count,
        sha256=sha256,
    )

    snapshot_path, manifest_path = _snapshot_paths(
        Path(root), metadata.dataset_id, acquired_at, sha256
    )
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot_path.exists() or manifest_path.exists():
        raise FileExistsError(f"refusing to overwrite existing snapshot: {snapshot_path}")

    with snapshot_path.open("xb") as handle:
        handle.write(payload)
    manifest_payload = json.dumps(
        manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )
    try:
        with manifest_path.open("x", encoding="utf-8") as manifest_handle:
            manifest_handle.write(f"{manifest_payload}\n")
    except OSError:
        # 清单以排他创建写入：并发写入者抢先落盘时不会覆盖其内容。
        # 失败则删除本次刚写入的快照，维持“快照必带清单”的不变式。
        snapshot_path.unlink(missing_ok=True)
        raise
    return snapshot_path, manifest_path, manifest

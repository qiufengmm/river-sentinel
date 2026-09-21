"""Official ``cata_12720`` water-level file ingestion, provenance and hourly grid.

本模块只做三件事，且职责边界严格：

1. :func:`read_official_water_level_records` 读取官方下载产物。温州市公共数据开放平台
   的「数据下载」虽然把文件命名为 ``*.csv``，但实体是 **ZIP 压缩包**，包内是若干分片
   ``cata_12720_<n>.csv``（GBK 编码）。函数把分片合并、按 ``主键`` 去重（保留末次），
   并把官方中文表头映射为 :mod:`river_sentinel_ml.normalize` 认识的官方键
   （``z_id`` / ``time_d`` / ``up_water_level``）。它不做清洗、排序、单位换算与时区转换。
2. :func:`load_official_water_level_file` 复用既有
   :func:`river_sentinel_ml.normalize.normalize_water_level_rows`，产出六列契约帧与
   :class:`QualitySummary` 审计，用于把「原始合并行数」与「可用行数」显式分开。
3. :func:`build_hourly_water_level` 复用既有
   :func:`river_sentinel_ml.dataset.resample_water_level`，生成 1 小时网格序列，
   为 DS-4b（小时特征）与 DS-5b（小时基线）提供唯一入口。

本模块不实现任何日聚合、滞后/滚动特征或模型；也不读取环境变量、不发起网络请求。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pandas as pd

from river_sentinel_ml.contracts import QualitySummary
from river_sentinel_ml.dataset import OUTPUT_COLUMNS as HOURLY_OUTPUT_COLUMNS
from river_sentinel_ml.dataset import resample_water_level
from river_sentinel_ml.normalize import normalize_water_level_rows

#: 允许落入适配器的文件扩展名。XLS/XLSX 需要额外解析库且本轮只作交叉核对，
#: 因此刻意不在白名单内：宁可显式失败，也不把二进制表悄悄地读进来。
SUPPORTED_SUFFIXES: Final[tuple[str, ...]] = (".zip", ".csv", ".json")

#: DS-1b 实测：官方 CSV 分片为 GBK（GB18030 的超集），表头 ``监测时间,实时水位,主键``。
DEFAULT_ENCODING: Final[str] = "gbk"
DEFAULT_TIMEZONE: Final[str] = "Asia/Shanghai"
DEFAULT_DATASET_ID: Final[str] = "12720"
DEFAULT_SOURCE_NAME: Final[str] = (
    "温州市公共数据开放平台 cata_12720（文成县飞云江二期治理工程水位信息）"
)
DEFAULT_SOURCE_URI: Final[str] = (
    "https://data.wenzhou.gov.cn/jdop_front/resource/data/download.do?fileType=csv&iid=12720"
)
DEFAULT_FREQUENCY: Final[str] = "1h"

#: 官方中文表头 → :mod:`river_sentinel_ml.normalize` 的官方键。
DEFAULT_CSV_COLUMN_MAPPING: Final[Mapping[str, str]] = {
    "监测时间": "time_d",
    "实时水位": "up_water_level",
    "主键": "z_id",
}

#: 映射后必须齐备的键；缺任何一个都无法构造可用观测。
REQUIRED_RECORD_KEYS: Final[tuple[str, ...]] = ("time_d", "up_water_level", "z_id")

#: 记录主键，用于分片边界去重。
RECORD_ID_KEY: Final[str] = "z_id"

_SHA256_CHUNK_BYTES: Final[int] = 1 << 20

#: ``max_interpolation_steps`` 的缺失哨兵：该阈值待 DS-2b 实测分布后裁定，
#: 因此不允许给默认值，漏传必须显式报错而不是悄悄取一个数。
_REQUIRED_STEPS: Final[object] = object()


@dataclass(frozen=True)
class FileProvenance:
    """One downloaded file, described well enough to be re-fetched and verified.

    Attributes:
        source_name: Human-readable data source name.
        source_uri: Concrete download endpoint or landing page.
        dataset_id: Platform dataset id, e.g. ``"12720"``.
        acquired_at: UTC acquisition time as ISO 8601 text. Derived from the file's
            modification time so repeated descriptions of the same file are stable.
        file_size_bytes: Size of the described file, in bytes.
        sha256: Streaming SHA-256 of the file content, lower-case hex.
        record_count: Number of records the adapter extracted from the file, which
            for a ZIP equals the merged and de-duplicated row count.
        file_format: Container format actually read, e.g. ``"zip"`` or ``"csv"``.
    """

    source_name: str
    source_uri: str
    dataset_id: str
    acquired_at: str
    file_size_bytes: int
    sha256: str
    record_count: int
    file_format: str


@dataclass(frozen=True)
class ArchiveInspection:
    """Merge diagnostics for one official archive, used for audit evidence.

    Attributes:
        path: The file that was inspected.
        file_format: Container format actually read.
        member_names: Every member inside the archive, in archive order.
        csv_member_names: Members treated as CSV shards, in archive order.
        rows_per_member: ``(member name, data rows)`` pairs for each CSV shard.
        total_rows: Rows read across all shards, before de-duplication.
        unique_rows: Rows surviving the primary-key de-duplication.
        duplicate_rows: Rows dropped because their primary key was seen again.
    """

    path: str
    file_format: str
    member_names: tuple[str, ...]
    csv_member_names: tuple[str, ...]
    rows_per_member: tuple[tuple[str, int], ...]
    total_rows: int
    unique_rows: int
    duplicate_rows: int


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def read_official_water_level_records(
    path: str | Path,
    *,
    encoding: str = DEFAULT_ENCODING,
    column_mapping: Mapping[str, str] = DEFAULT_CSV_COLUMN_MAPPING,
) -> list[dict[str, object]]:
    """Read raw ``cata_12720`` records from a downloaded file, without cleaning.

    Args:
        path: A ``.zip`` (ZIP containing CSV shards), ``.csv`` or ``.json`` file.
            ZIP is the real shape of the platform download even when the file is
            named ``*.csv``.
        encoding: Text encoding of the payload. DS-1b measured the official shards
            as GBK, so that is the default. Decoding is strict: an undecodable byte
            raises instead of being replaced, because a mangled numeric column would
            silently corrupt water levels.
        column_mapping: Official header -> official key mapping. Only mapped columns
            are kept; no column name is ever guessed.

    Returns:
        Records using the official keys ``z_id`` / ``time_d`` / ``up_water_level``,
        with every value kept as raw text. Records are merged from all shards and
        de-duplicated by ``z_id`` keeping the **last** occurrence, because shards
        overlap at their boundaries. Nothing is sorted, unit-converted or localized;
        that belongs to :func:`load_official_water_level_file`.

    Raises:
        ValueError: The suffix is not supported, the file is missing or empty, the
            ZIP holds no CSV shard, a payload cannot be decoded with ``encoding``,
            or a shard lacks one of :data:`REQUIRED_RECORD_KEYS` after mapping.
    """
    target = Path(path)
    suffix = _validated_suffix(target)
    _require_readable_file(target)

    if suffix == ".zip" or zipfile.is_zipfile(target):
        # 平台把 ZIP 命名为 ``*.csv``（DS-1b 实测），因此容器按**内容**识别：
        # 只看扩展名会把这个数据集整份读成一个二进制 CSV。
        payloads = _read_zip_documents(target, encoding)
    elif suffix == ".csv":
        payloads = [(target.name, _decode(target.read_bytes(), encoding, str(target)))]
    else:
        return _read_json_records(target, encoding, column_mapping)

    rows, _, _, _ = _merge_csv_documents(payloads, column_mapping, str(target))
    return rows


def inspect_official_archive(
    path: str | Path,
    *,
    encoding: str = DEFAULT_ENCODING,
    column_mapping: Mapping[str, str] = DEFAULT_CSV_COLUMN_MAPPING,
) -> ArchiveInspection:
    """Return merge diagnostics for one official file, including duplicate evidence.

    This exists because :func:`read_official_water_level_records` returns records
    only, while the report must be able to show shard names, per-shard row counts
    and how many records were dropped as cross-shard duplicates. It performs the
    same merge, so its numbers describe the very same rows.
    """
    target = Path(path)
    suffix = _validated_suffix(target)
    _require_readable_file(target)

    if suffix == ".zip" or zipfile.is_zipfile(target):
        payloads = _read_zip_documents(target, encoding)
        member_names = tuple(name for name, _ in payloads)
        container = "zip"
    elif suffix == ".csv":
        payloads = [(target.name, _decode(target.read_bytes(), encoding, str(target)))]
        member_names = (target.name,)
    else:
        rows = _read_json_records(target, encoding, column_mapping)
        return ArchiveInspection(
            path=str(target),
            file_format="json",
            member_names=(target.name,),
            csv_member_names=(),
            rows_per_member=((target.name, len(rows)),),
            total_rows=len(rows),
            unique_rows=len(rows),
            duplicate_rows=0,
        )

    rows, per_member, total, duplicates = _merge_csv_documents(
        payloads, column_mapping, str(target)
    )
    return ArchiveInspection(
        path=str(target),
        file_format=container,
        member_names=member_names,
        csv_member_names=tuple(name for name, _ in payloads),
        rows_per_member=tuple(per_member),
        total_rows=total,
        unique_rows=len(rows),
        duplicate_rows=duplicates,
    )


def load_official_water_level_file(
    path: str | Path,
    *,
    source_uri: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
    encoding: str = DEFAULT_ENCODING,
    column_mapping: Mapping[str, str] = DEFAULT_CSV_COLUMN_MAPPING,
) -> tuple[pd.DataFrame, QualitySummary]:
    """Load one official file into the normalized six-column contract frame.

    The cleaning itself is delegated to the frozen
    :func:`river_sentinel_ml.normalize.normalize_water_level_rows`: this function
    only reads and maps, so the counting口径 stays identical to every other source.

    Args:
        path: See :func:`read_official_water_level_records`.
        source_uri: Optional endpoint shown in provenance; unused by normalization.
        timezone: IANA zone that naive source timestamps are localized to.
        encoding: Payload encoding, see :func:`read_official_water_level_records`.
        column_mapping: Official header -> official key mapping.

    Returns:
        The normalized frame with exactly six columns (``source_record_id``,
        ``observed_at``, ``water_level_m``, ``station_code``, ``station_name``,
        ``quality_flag``) and the :class:`QualitySummary` audit, whose conservation
        identity ``total_rows == valid_rows + invalid_timestamp_rows
        + invalid_level_rows + missing_level_rows + duplicate_rows`` always holds.
    """
    del source_uri  # provenance is produced separately by describe_downloaded_file.
    rows = read_official_water_level_records(path, encoding=encoding, column_mapping=column_mapping)
    return normalize_water_level_rows(rows, timezone)


def build_hourly_water_level(
    frame: pd.DataFrame,
    *,
    frequency: str = DEFAULT_FREQUENCY,
    max_interpolation_steps: object = _REQUIRED_STEPS,
) -> pd.DataFrame:
    """Aggregate a normalized frame onto an evenly spaced grid.

    Thin, contract-preserving wrapper around the frozen
    :func:`river_sentinel_ml.dataset.resample_water_level`: the implementation is
    neither copied nor rewritten here, so bucket semantics stay exactly as Task 6
    defined them (last observation inside the bucket, ``observed_at`` is the bucket's
    left edge, one-bucket-wide bounded look-ahead, short gaps interpolated and
    flagged ``is_imputed=True`` / ``quality_flag="imputed"``).

    Args:
        frame: Normalized frame carrying timezone-aware ``observed_at`` and numeric
            ``water_level_m``.
        frequency: Pandas frequency alias for the bucket width; hourly by design.
        max_interpolation_steps: Largest consecutive missing step count that may be
            linearly interpolated. There is deliberately **no default**: the
            threshold is decided from the DS-2b gap-length distribution, so omitting
            it fails loudly instead of silently picking a number.

    Returns:
        A frame whose column set is exactly
        :data:`river_sentinel_ml.dataset.OUTPUT_COLUMNS`, one row per grid step,
        sorted by ``observed_at`` with a default integer index.

    Raises:
        ValueError: ``max_interpolation_steps`` is missing, or not a non-negative
            integer, or anything :func:`resample_water_level` rejects.
    """
    if max_interpolation_steps is _REQUIRED_STEPS:
        raise ValueError(
            "max_interpolation_steps is required: pass a non-negative integer number "
            "of missing steps that may be interpolated (0 disables interpolation)"
        )
    return resample_water_level(
        frame,
        frequency=frequency,
        max_interpolation_steps=max_interpolation_steps,
    )


def describe_downloaded_file(
    path: str | Path,
    *,
    source_uri: str,
    dataset_id: str = DEFAULT_DATASET_ID,
    source_name: str = DEFAULT_SOURCE_NAME,
    file_format: str | None = None,
    encoding: str = DEFAULT_ENCODING,
    column_mapping: Mapping[str, str] = DEFAULT_CSV_COLUMN_MAPPING,
) -> FileProvenance:
    """Describe one immutable downloaded file for the governance record.

    The digest is streamed in chunks, so a several-megabyte archive is never held in
    memory twice. ``record_count`` counts what the adapter actually merged and
    de-duplicated, which for a ZIP differs from the raw shard row total; both numbers
    are available through :func:`inspect_official_archive`.

    Args:
        path: The downloaded file.
        source_uri: Concrete endpoint the file came from.
        dataset_id: Platform dataset id.
        source_name: Human-readable source name.
        file_format: Actual container format; derived from the suffix when omitted.
        encoding: Payload encoding used when counting records.
        column_mapping: Official header -> official key mapping.

    Returns:
        The :class:`FileProvenance` record.

    Raises:
        ValueError: The suffix is not supported, or the path is missing / not a file.
    """
    target = Path(path)
    suffix = _validated_suffix(target)
    _require_existing_file(target)

    digest = hashlib.sha256()
    size = 0
    with target.open("rb") as handle:
        while True:
            chunk = handle.read(_SHA256_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)

    rows = read_official_water_level_records(
        target, encoding=encoding, column_mapping=column_mapping
    )
    modified = datetime.fromtimestamp(target.stat().st_mtime, UTC)
    return FileProvenance(
        source_name=source_name,
        source_uri=source_uri,
        dataset_id=dataset_id,
        acquired_at=modified.strftime("%Y-%m-%dT%H:%M:%SZ"),
        file_size_bytes=size,
        sha256=digest.hexdigest(),
        record_count=len(rows),
        file_format=file_format or ("zip" if zipfile.is_zipfile(target) else suffix.lstrip(".")),
    )


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _validated_suffix(target: Path) -> str:
    """Return the lower-cased suffix, or fail with an actionable message."""
    suffix = target.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(SUPPORTED_SUFFIXES)
        raise ValueError(
            f"unsupported file type {target.suffix!r} for {target}: expected one of {supported}"
        )
    return suffix


def _require_existing_file(target: Path) -> None:
    """Reject a missing path or a non-regular file before any content is touched."""
    if not target.exists():
        raise ValueError(f"official water level file does not exist: {target}")
    if not target.is_file():
        raise ValueError(f"official water level path is not a file: {target}")


def _require_readable_file(target: Path) -> None:
    """Reject a missing, non-regular or empty file."""
    _require_existing_file(target)
    if target.stat().st_size == 0:
        raise ValueError(f"official water level file is empty: {target}")


def _decode(payload: bytes, encoding: str, location: str) -> str:
    """Decode bytes strictly, reporting the encoding and location on failure.

    Replacing undecodable bytes would turn a corrupted numeric column into plausible
    numbers, which no downstream check can detect afterwards.
    """
    try:
        return payload.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"cannot decode {location} with encoding {encoding!r}: {exc.reason} at byte {exc.start}"
        ) from exc


# --------------------------------------------------------------------------- #
# Container readers
# --------------------------------------------------------------------------- #


def _read_zip_documents(target: Path, encoding: str) -> list[tuple[str, str]]:
    """Read every CSV shard of a ZIP as text, failing when there is none."""
    if not zipfile.is_zipfile(target):
        raise ValueError(f"{target} is named like a ZIP but is not a readable ZIP archive")
    with zipfile.ZipFile(target) as archive:
        members = [
            (info.filename, info)
            for info in archive.infolist()
            if not info.is_dir() and Path(info.filename).suffix.lower() == ".csv"
        ]
        if not members:
            raise ValueError(f"{target} contains no CSV shard; nothing can be merged")
        return [
            (name, _decode(archive.read(info), encoding, f"{target}!{name}"))
            for name, info in members
        ]


def _read_json_records(
    target: Path,
    encoding: str,
    column_mapping: Mapping[str, str],
) -> list[dict[str, object]]:
    """Read a JSON array of official records and map its keys."""
    text = _decode(target.read_bytes(), encoding, str(target))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{target} does not contain valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError(
            f"{target} must contain a JSON array of records, got {type(payload).__name__}"
        )
    rows: list[dict[str, object]] = []
    for index, record in enumerate(payload):
        if not isinstance(record, Mapping):
            raise ValueError(
                f"record at index {index} in {target} must be an object, got {type(record).__name__}"
            )
        mapped = _map_row(record, column_mapping)
        rows.append(mapped)
    return rows


# --------------------------------------------------------------------------- #
# CSV merging
# --------------------------------------------------------------------------- #


def _merge_csv_documents(
    payloads: Sequence[tuple[str, str]],
    column_mapping: Mapping[str, str],
    location: str,
) -> tuple[list[dict[str, object]], list[tuple[str, int]], int, int]:
    """Merge decoded CSV documents into de-duplicated official records.

    Shards may repeat their header and may order their columns differently, so every
    document is parsed on its own and mapped independently. Records whose ``z_id`` was
    already seen are replaced by the later occurrence: shard boundaries overlap, and
    keeping the first copy would silently prefer whichever shard came first in the
    archive. Nothing else is filtered here.

    Returns:
        ``(records, rows per member, rows read before dedupe, rows dropped as
        duplicates)``.
    """
    merged: dict[str, dict[str, object]] = {}
    order: list[str] = []
    per_member: list[tuple[str, int]] = []
    total_rows = 0
    anonymous_seen = 0

    for name, text in payloads:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = reader.fieldnames or []
        _require_official_keys(headers, f"{location}!{name}", column_mapping)
        rows_here = 0
        for raw_row in reader:
            row = _map_row(raw_row, column_mapping)
            rows_here += 1
            key = row.get(RECORD_ID_KEY)
            identifier = "" if key is None else str(key).strip()
            if not identifier:
                # Without a primary key the row cannot be de-duplicated or traced, so
                # it is passed through untouched and left for normalization to count.
                anonymous_seen += 1
                slot = f"\x00anon{anonymous_seen}"
                order.append(slot)
                merged[slot] = row
                continue
            if identifier not in merged:
                order.append(identifier)
            merged[identifier] = row
        per_member.append((name, rows_here))
        total_rows += rows_here

    records = [merged[key] for key in order]
    return records, per_member, total_rows, total_rows - len(merged)


def _map_row(row: Mapping[str, object], column_mapping: Mapping[str, str]) -> dict[str, object]:
    """Rename official headers to official keys, dropping every unmapped column."""
    mapped: dict[str, object] = {}
    for key, value in row.items():
        if key is None:
            continue
        source = str(key).strip().strip('"').strip()
        target = column_mapping.get(source)
        if target is not None:
            mapped[target] = value
    return mapped


def _require_official_keys(
    headers: Sequence[str],
    location: str,
    column_mapping: Mapping[str, str],
) -> None:
    """Fail when a shard cannot supply every required official key."""
    available = {column_mapping.get(str(name).strip().strip('"').strip()) for name in headers}
    missing = [key for key in REQUIRED_RECORD_KEYS if key not in available]
    if missing:
        listed = ", ".join(missing)
        raise ValueError(f"{location} is missing required column(s) after mapping: {listed}")


__all__ = [
    "HOURLY_OUTPUT_COLUMNS",
    "DEFAULT_CSV_COLUMN_MAPPING",
    "DEFAULT_ENCODING",
    "DEFAULT_FREQUENCY",
    "DEFAULT_SOURCE_NAME",
    "DEFAULT_SOURCE_URI",
    "DEFAULT_TIMEZONE",
    "REQUIRED_RECORD_KEYS",
    "SUPPORTED_SUFFIXES",
    "ArchiveInspection",
    "FileProvenance",
    "build_hourly_water_level",
    "describe_downloaded_file",
    "inspect_official_archive",
    "load_official_water_level_file",
    "read_official_water_level_records",
]

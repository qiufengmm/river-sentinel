"""Normalize raw water-level rows into the analysis contract.

This module turns rows that still use the official Wenzhou field layout
(``z_id``, ``time_d``, ``up_water_level``) into the tidy frame expected by the
downstream sequence builder, and audits how many rows survived.

The transformation is a pure function: it never reads files, never accesses the
network, never writes to disk, and never imputes, fills or forward-fills a
missing level. Rows that cannot be trusted are dropped and counted instead.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

import pandas as pd

from river_sentinel_ml.contracts import QualitySummary
from river_sentinel_ml.quality import infer_interval_minutes

OUTPUT_COLUMNS: tuple[str, ...] = (
    "source_record_id",
    "observed_at",
    "water_level_m",
    "station_code",
    "station_name",
    "quality_flag",
)

SOURCE_ID_KEY = "z_id"
SOURCE_TIME_KEY = "time_d"
SOURCE_LEVEL_KEY = "up_water_level"

NORMAL_QUALITY_FLAG = "normal"
LEVEL_OK = "ok"
LEVEL_MISSING = "missing"
LEVEL_INVALID = "invalid"


class _Candidate(NamedTuple):
    """One row that passed every validation and may still be deduplicated."""

    record_id: str
    observed_at: pd.Timestamp
    water_level_m: float
    station_code: str | None
    station_name: str | None


def normalize_water_level_rows(
    rows: Iterable[Mapping[str, object]],
    timezone: str,
) -> tuple[pd.DataFrame, QualitySummary]:
    """Normalize raw water-level rows and audit how many of them survive.

    Args:
        rows: Raw records using the official keys ``z_id``, ``time_d`` and
            ``up_water_level``, with optional ``station_code``/``station_name``.
            Any iterable of mappings is accepted, including generators.
        timezone: IANA name (for example ``"Asia/Shanghai"``) that naive source
            timestamps are localized to, and that aware timestamps are converted
            to.

    Returns:
        A frame whose column set is exactly ``OUTPUT_COLUMNS``, sorted by
        ``observed_at`` then ``source_record_id`` with a default integer index,
        together with the :class:`QualitySummary` audit of the batch.

    Rejection pipeline and counting口径 (frozen):

    1. ``z_id`` empty / missing -> the row is unusable.
    2. ``time_d`` unparsable, or a local time that is ambiguous or nonexistent
       in ``timezone`` -> ``invalid_timestamp_rows``.
    3. ``up_water_level`` empty -> ``missing_level_rows``; present but not a
       finite number -> ``invalid_level_rows``.
    4. Repeated ``source_record_id`` among surviving rows -> ``duplicate_rows``,
       keeping the **last** valid occurrence.

    The four counters are mutually exclusive and evaluated in that order, so
    ``total_rows == valid_rows + invalid_timestamp_rows + invalid_level_rows
    + missing_level_rows + duplicate_rows`` always holds.

    Known deviation: ``QualitySummary`` has no dedicated bucket for an unusable
    ``z_id``, so such rows are counted in ``invalid_timestamp_rows`` to keep the
    conservation identity exact. This is deliberate and must stay documented.
    """
    tz = ZoneInfo(timezone)
    total_rows = 0
    invalid_timestamp_rows = 0
    invalid_level_rows = 0
    missing_level_rows = 0
    duplicate_rows = 0
    kept: dict[str, _Candidate] = {}

    for row in rows:
        total_rows += 1

        record_id = _to_record_id(row.get(SOURCE_ID_KEY))
        if record_id is None:
            invalid_timestamp_rows += 1
            continue

        observed_at = _parse_timestamp(row.get(SOURCE_TIME_KEY), tz)
        if observed_at is None:
            invalid_timestamp_rows += 1
            continue

        level, state = _classify_level(row.get(SOURCE_LEVEL_KEY))
        if state == LEVEL_MISSING:
            missing_level_rows += 1
            continue
        if state == LEVEL_INVALID or level is None:
            invalid_level_rows += 1
            continue

        if record_id in kept:
            duplicate_rows += 1
        kept[record_id] = _Candidate(
            record_id=record_id,
            observed_at=observed_at,
            water_level_m=level,
            station_code=_to_optional_text(row.get("station_code")),
            station_name=_to_optional_text(row.get("station_name")),
        )

    frame = _build_frame(kept.values(), timezone)
    summary = _summarize(
        frame=frame,
        total_rows=total_rows,
        duplicate_rows=duplicate_rows,
        invalid_timestamp_rows=invalid_timestamp_rows,
        invalid_level_rows=invalid_level_rows,
        missing_level_rows=missing_level_rows,
    )
    return frame, summary


def _build_frame(candidates: Iterable[_Candidate], timezone: str) -> pd.DataFrame:
    """Materialize candidates into the contract frame, typed even when empty."""
    items = list(candidates)
    frame = pd.DataFrame(
        {
            "source_record_id": pd.Series([item.record_id for item in items], dtype="object"),
            "observed_at": pd.Series(
                [item.observed_at for item in items], dtype=f"datetime64[ns, {timezone}]"
            ),
            "water_level_m": pd.Series([item.water_level_m for item in items], dtype="float64"),
            "station_code": pd.Series([item.station_code for item in items], dtype="object"),
            "station_name": pd.Series([item.station_name for item in items], dtype="object"),
            "quality_flag": pd.Series([NORMAL_QUALITY_FLAG] * len(items), dtype="object"),
        }
    )
    return frame.sort_values(["observed_at", "source_record_id"], kind="stable").reset_index(
        drop=True
    )


def _summarize(
    *,
    frame: pd.DataFrame,
    total_rows: int,
    duplicate_rows: int,
    invalid_timestamp_rows: int,
    invalid_level_rows: int,
    missing_level_rows: int,
) -> QualitySummary:
    """Build the audit summary, leaving coverage empty for an empty frame."""
    start_at: datetime | None = None
    end_at: datetime | None = None
    interval: float | None = None

    if not frame.empty:
        index = pd.DatetimeIndex(frame["observed_at"])
        start_at = index.min().to_pydatetime()
        end_at = index.max().to_pydatetime()
        interval = infer_interval_minutes(index)

    return QualitySummary(
        total_rows=total_rows,
        valid_rows=len(frame),
        duplicate_rows=duplicate_rows,
        invalid_timestamp_rows=invalid_timestamp_rows,
        invalid_level_rows=invalid_level_rows,
        missing_level_rows=missing_level_rows,
        start_at=start_at,
        end_at=end_at,
        inferred_interval_minutes=interval,
    )


def _to_record_id(value: object) -> str | None:
    """Convert ``z_id`` to a non-empty string, or ``None`` when it is unusable."""
    if value is None:
        return None
    if isinstance(value, float):
        # Integral floats such as 1001.0 must keep their integer identity.
        if math.isnan(value):
            return None
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str):
        return value.strip() or None
    return _to_optional_text(value)


def _to_optional_text(value: object) -> str | None:
    """Render an optional field as text, mapping anything empty to ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        # Exotic objects that pandas cannot test for NA: fall back to str().
        pass
    return str(value)


def _parse_timestamp(value: object, tz: ZoneInfo) -> pd.Timestamp | None:
    """Parse a source timestamp into ``tz``, or ``None`` when it is unusable."""
    moment = _coerce_moment(value)
    if moment is None:
        return None
    if moment.tzinfo is not None and moment.tzinfo.utcoffset(moment) is not None:
        return pd.Timestamp(moment).tz_convert(tz)
    return _localize(moment, tz)


def _coerce_moment(value: object) -> datetime | None:
    """Parse a source timestamp cell into a ``datetime`` without touching zones."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.to_pydatetime()
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # Not strict ISO: fall back to the parser used by the ingestion layer.
        pass
    try:
        moment = pd.Timestamp(text)
    except (ValueError, TypeError):
        return None
    return None if pd.isna(moment) else moment.to_pydatetime()


def _localize(moment: datetime, tz: ZoneInfo) -> pd.Timestamp | None:
    """Attach ``tz`` to a naive wall time, rejecting ambiguous and gap times."""
    before = moment.replace(tzinfo=tz, fold=0)
    after = moment.replace(tzinfo=tz, fold=1)
    if before.utcoffset() != after.utcoffset():
        # Either a DST gap (the wall time does not exist) or a DST overlap (it
        # happens twice). Never guess an offset: the row is unusable.
        return None
    return pd.Timestamp(before)


def _classify_level(value: object) -> tuple[float | None, str]:
    """Classify a source level cell as ``ok``, ``missing`` or ``invalid``."""
    if value is None:
        return None, LEVEL_MISSING
    if isinstance(value, bool):
        return None, LEVEL_INVALID

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, LEVEL_MISSING
        try:
            number = float(text)
        except ValueError:
            return None, LEVEL_INVALID
    else:
        try:
            if bool(pd.isna(value)):
                return None, LEVEL_MISSING
            number = float(value)
        except (TypeError, ValueError):
            return None, LEVEL_INVALID

    if math.isnan(number):
        return None, LEVEL_MISSING
    if not math.isfinite(number):
        return None, LEVEL_INVALID
    return number, LEVEL_OK

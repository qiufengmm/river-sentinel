"""Batch fetch raw hourly rainfall from the Open-Meteo Historical Weather API.

用途：为文成县 5 个已冻结网格点批量抓取**原始小时级**降雨，写入不可变原始快照与
小时表，并可对已落盘的小时表做质量统计。

**本轮明确不做**：日聚合、6 小时聚合、滚动累计、滞后特征或任何特征工程；
落盘保留原始小时时间戳与 ``point_id``，使后续可自由选择日 / 6 小时 / 小时尺度。

数据口径：Open-Meteo Historical Weather API 的 **Best Match** 网格化再分析降雨（默认不发送
`models` 查询参数，响应不声明具体构成模型，待确认），不是文成县地面雨量站实测值，也不得表述为
「ERA5-Land 实测」；多点区域平均只能作为「文成县区域降雨代理变量」。本脚本产出仅用于教学科研与
辅助分析，不替代水行政主管部门的正式监测、预警、调度或应急决策。

用法示例：

```powershell
$env:PYTHONPATH = "$PWD\\ml\\src"
python scripts/fetch_open_meteo_archive.py --start-date 2015-01-01
python scripts/fetch_open_meteo_archive.py --summarize-only
```
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_SOURCE_ROOT: Final[Path] = _REPO_ROOT / "ml" / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from river_sentinel_ml.ingestion import open_meteo_snapshots  # noqa: E402
from river_sentinel_ml.ingestion.open_meteo import (  # noqa: E402
    DEFAULT_TIMEZONE,
    GridPoint,
    OpenMeteoClient,
    RainfallFetchError,
)

# DS-1 已核实并冻结的 5 个网格点（WGS-84；来源 OpenStreetMap Nominatim，
# OSM relation 3289421，访问日期 2026-09-21，逐点逆向地理编码确认落在文成县内）。
# 本工作树基线不含 data/metadata/wencheng_grid_points.csv，故在此内联同一组冻结值。
GRID_POINTS: Final[tuple[GridPoint, ...]] = (
    GridPoint(point_id="WC-G1", longitude=120.03, latitude=27.79),
    GridPoint(point_id="WC-G2", longitude=119.85, latitude=27.92),
    GridPoint(point_id="WC-G3", longitude=120.15, latitude=27.90),
    GridPoint(point_id="WC-G4", longitude=120.00, latitude=27.67),
    GridPoint(point_id="WC-G5", longitude=120.18, latitude=27.72),
)

DEFAULT_ROOT: Final[Path] = _REPO_ROOT / "data" / "raw"
EXPECTED_OFFSET: Final[timedelta] = timedelta(hours=8)
# 4xx 属于“请求本身有问题”，退避重试毫无意义，直接失败。
NO_RETRY_STATUS: Final[frozenset[int]] = frozenset({400, 401, 403, 404, 405, 422})


def _year_chunks(start_date: date, end_date: date) -> list[tuple[str, str]]:
    """Split the requested range into one chunk per calendar year (点 × 年)."""
    if end_date < start_date:
        raise ValueError(f"end_date must not be earlier than start_date: {start_date} > {end_date}")
    chunks: list[tuple[str, str]] = []
    for year in range(start_date.year, end_date.year + 1):
        chunk_start = max(start_date, date(year, 1, 1))
        chunk_end = min(end_date, date(year, 12, 31))
        chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
    return chunks


def _decode_failure(error: RuntimeError) -> dict[str, object]:
    """Decode a ``_fetch_chunk`` failure; tolerate messages that are not JSON."""
    try:
        decoded = json.loads(str(error))
    except ValueError:
        return {"error_type": type(error).__name__, "message": str(error), "attempts": None}
    return decoded if isinstance(decoded, dict) else {"message": str(error)}


def _status_code(error: RainfallFetchError) -> int | None:
    """Read the HTTP status from the chained httpx error without printing any URL."""
    cause = error.__cause__
    response = getattr(cause, "response", None)
    return getattr(response, "status_code", None)


def _rows_sidecar_path(hourly_path: Path) -> Path:
    """Resume receipt path: ``<stem>.rows.json`` next to the hourly table."""
    return hourly_path.parent / f"{hourly_path.stem}.rows.json"


def _write_rows_sidecar(hourly_path: Path, rows: int, expected: int) -> Path:
    sidecar = _rows_sidecar_path(hourly_path)
    sidecar.write_text(
        json.dumps(
            {
                "hourly_path": str(hourly_path),
                "record_count": rows,
                "expected_hours": expected,
                "complete": rows > 0 and rows == expected,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return sidecar


def _resume_state(hourly_path: Path, expected: int) -> tuple[bool, str]:
    """断点续跑判定：只有「有文件 + 行数 > 0 + 等于期望小时数 + 凭据未过期」才算已完成。

    小时表本身是最终权威：`<stem>.rows.json` 只是凭据，二者不一致时以小时表为准并重取。
    """
    if not hourly_path.exists():
        return False, "missing"
    rows = max(sum(1 for _ in hourly_path.open(encoding="utf-8", errors="replace")) - 1, 0)
    sidecar = _rows_sidecar_path(hourly_path)
    sidecar_rows: int | None = None
    if sidecar.exists():
        try:
            sidecar_rows = int(json.loads(sidecar.read_text(encoding="utf-8"))["record_count"])
        except (ValueError, KeyError, TypeError):
            sidecar_rows = None
    if rows <= 0:
        return False, f"empty(table_rows={rows})"
    if expected and rows != expected:
        return False, f"incomplete(table_rows={rows}, expected={expected})"
    if sidecar_rows is not None and sidecar_rows != rows:
        return False, f"stale_sidecar(table_rows={rows}, sidecar_rows={sidecar_rows})"
    return True, f"complete(table_rows={rows})"


def _fetch_chunk(
    client: OpenMeteoClient,
    point: GridPoint,
    start: str,
    end: str,
    *,
    models: str | None,
    root: Path,
    sleep_seconds: float,
    max_retries: int,
) -> tuple[int, dict[str, object]]:
    """Fetch one point × time chunk with bounded retries; never fabricate data."""
    hourly_path = root / "open-meteo" / "hourly" / point.point_id / f"{start}_{end}.csv"
    for attempt in range(1, max_retries + 1):
        try:
            result = client.fetch_hourly_rainfall(
                [point], start, end, models=models, snapshot_root=root
            )
        except RainfallFetchError as error:
            status = _status_code(error)
            if status in NO_RETRY_STATUS:
                raise RuntimeError(
                    json.dumps(
                        {
                            "error_type": type(error).__name__,
                            "exception_type": type(error.__cause__).__name__
                            if error.__cause__ is not None
                            else None,
                            "status_code": status,
                            "message": str(error),
                            "attempts": attempt,
                            "retried": False,
                        },
                        ensure_ascii=False,
                    )
                ) from None
            if attempt >= max_retries:
                raise RuntimeError(
                    json.dumps(
                        {
                            "error_type": type(error).__name__,
                            "exception_type": type(error.__cause__).__name__
                            if error.__cause__ is not None
                            else None,
                            "status_code": status,
                            "message": str(error),
                            "attempts": attempt,
                        },
                        ensure_ascii=False,
                    )
                ) from None
            time.sleep(sleep_seconds * (2 ** (attempt - 1)))
            continue
        except httpx.HTTPError as error:  # pragma: no cover - defensive
            if attempt >= max_retries:
                raise RuntimeError(
                    json.dumps(
                        {
                            "error_type": type(error).__name__,
                            "status_code": None,
                            "message": "http transport failure",
                            "attempts": attempt,
                        },
                        ensure_ascii=False,
                    )
                ) from None
            time.sleep(sleep_seconds * (2 ** (attempt - 1)))
            continue

        open_meteo_snapshots.write_hourly_table(result.frame, point.point_id, start, end, root)
        rows = len(result.frame)
        expected = _expected_hours(start, end)
        sidecar = _write_rows_sidecar(hourly_path, rows, expected)
        return rows, {
            "snapshot_paths": [str(path) for path in result.raw_response_paths],
            "hourly_path": str(hourly_path),
            "rows_sidecar_path": str(sidecar),
            "expected_hours": expected,
            "sha256": result.provenance.sha256,
            "attempts": attempt,
        }

    raise RuntimeError("unreachable retry state")  # pragma: no cover


def fetch_archive(
    *,
    root: Path,
    start_date: date,
    end_date: date,
    points: tuple[GridPoint, ...],
    models: str | None,
    sleep_seconds: float,
    max_retries: int,
    force: bool,
) -> dict[str, object]:
    """Fetch every point × year chunk and return a machine-readable run summary."""
    chunks = _year_chunks(start_date, end_date)
    started_at = datetime.now(tz=ZoneInfo(DEFAULT_TIMEZONE))
    records: list[dict[str, object]] = []

    with OpenMeteoClient() as client:
        for point in points:
            for start, end in chunks:
                hourly_path = root / "open-meteo" / "hourly" / point.point_id / f"{start}_{end}.csv"
                expected = _expected_hours(start, end)
                entry: dict[str, object] = {
                    "point_id": point.point_id,
                    "start_date": start,
                    "end_date": end,
                    "expected_hours": expected,
                    "status": "pending",
                }
                # 续跑凭据：仅当「文件存在 + 行数 > 0 + 等于期望小时数」时才跳过。
                complete, reason = _resume_state(hourly_path, expected)
                if complete and not force:
                    entry["status"] = "skipped_existing"
                    entry["hourly_path"] = str(hourly_path)
                    entry["resume_reason"] = reason
                    records.append(entry)
                    print(f"[skip] {point.point_id} {start}..{end} {reason}")
                    continue
                if hourly_path.exists() and not force:
                    print(f"[redo] {point.point_id} {start}..{end} {reason}")

                try:
                    rows, detail = _fetch_chunk(
                        client,
                        point,
                        start,
                        end,
                        models=models,
                        root=root,
                        sleep_seconds=sleep_seconds,
                        max_retries=max_retries,
                    )
                except RuntimeError as error:
                    failure = _decode_failure(error)
                    entry["status"] = "failed"
                    entry.update(failure)
                    records.append(entry)
                    print(f"[FAIL] {point.point_id} {start}..{end} {failure}")
                    continue
                except FileExistsError as error:
                    entry["status"] = "already_stored"
                    entry["message"] = str(error)
                    entry["hourly_path"] = str(hourly_path)
                    records.append(entry)
                    print(f"[keep] {point.point_id} {start}..{end} snapshot already present")
                    continue

                entry["status"] = "ok"
                entry["record_count"] = rows
                entry.update(detail)
                if rows == 0:
                    entry["empty"] = True
                    print(f"[WARN] {point.point_id} {start}..{end} rows=0（空响应，需复核）")
                else:
                    print(f"[ ok ] {point.point_id} {start}..{end} rows={rows}")
                time.sleep(sleep_seconds)

    summary: dict[str, object] = {
        "generated_at": started_at.isoformat(),
        "source": (
            "Open-Meteo Historical Weather API (Best Match gridded reanalysis rainfall; "
            "response does not declare the contributing model)"
        ),
        "root": str(root),
        "models": models,
        "models_explicit": models is not None,
        "model_selection": "api_default_best_match" if models is None else "explicit_models",
        "timezone": DEFAULT_TIMEZONE,
        "requested_start_date": start_date.isoformat(),
        "requested_end_date": end_date.isoformat(),
        "points": [
            {
                "point_id": point.point_id,
                "longitude": point.longitude,
                "latitude": point.latitude,
            }
            for point in points
        ],
        "chunk_count": len(records),
        "ok": sum(1 for item in records if item["status"] == "ok"),
        "failed": [item for item in records if item["status"] == "failed"],
        "empty": [item for item in records if item.get("empty")],
        "chunks": records,
    }
    return summary


def _expected_hours(start_date: str, end_date: str) -> int:
    """Natural hour count of a closed day range in ``Asia/Shanghai`` (no DST)."""
    zone = ZoneInfo(DEFAULT_TIMEZONE)
    start = datetime.combine(date.fromisoformat(start_date), datetime.min.time(), tzinfo=zone)
    end = datetime.combine(date.fromisoformat(end_date), datetime.min.time(), tzinfo=zone)
    return int((end - start).total_seconds() // 3600) + 24


def _chunk_statistics(path: Path, point_id: str, start: str, end: str) -> dict[str, object]:
    frame = pd.read_csv(path)
    observed = pd.to_datetime(frame["observed_at"], format="ISO8601")
    precipitation = frame["precipitation_mm"].astype("float64")
    rain = frame["rain_mm"].astype("float64")

    expected = _expected_hours(start, end)
    unique_count = int(observed.nunique())
    offsets = sorted({value.utcoffset() for value in observed if value.utcoffset() is not None})
    differ = ~(precipitation.eq(rain) | (precipitation.isna() & rain.isna()))
    difference = (precipitation - rain).abs()
    first_difference = None
    if bool(differ.any()):
        first_difference = observed[differ].iloc[0].isoformat()

    return {
        "point_id": point_id,
        "start_date": start,
        "end_date": end,
        "rows": int(len(frame)),
        "expected_hours": expected,
        "missing_hours": int(max(expected - unique_count, 0)),
        "missing_rate": round(float(max(expected - unique_count, 0) / expected), 6)
        if expected
        else None,
        "duplicate_timestamps": int(len(observed) - unique_count),
        "non_finite_precipitation": int(precipitation.isna().sum()),
        "non_finite_rain": int(rain.isna().sum()),
        "hour_aligned": bool(((observed.dt.minute == 0) & (observed.dt.second == 0)).all()),
        "utc_offsets": [str(offset) for offset in offsets],
        "offset_always_plus_08": offsets == [EXPECTED_OFFSET],
        "precipitation_rain_diff_rows": int(differ.sum()),
        "precipitation_rain_max_abs_diff": float(difference.max())
        if len(difference) and difference.notna().any()
        else None,
        "precipitation_rain_first_diff_at": first_difference,
        "start_timestamp": observed.iloc[0].isoformat() if len(observed) else None,
        "end_timestamp": observed.iloc[-1].isoformat() if len(observed) else None,
    }


def summarize(root: Path, points: tuple[GridPoint, ...]) -> dict[str, object]:
    """Read the hourly tables back and report per point × chunk quality statistics."""
    statistics: list[dict[str, object]] = []
    for point in points:
        directory = root / "open-meteo" / "hourly" / point.point_id
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.csv")):
            start, end = path.stem.split("_", 1)
            statistics.append(_chunk_statistics(path, point.point_id, start, end))

    total_rows = sum(int(item["rows"]) for item in statistics)
    total_expected = sum(int(item["expected_hours"]) for item in statistics)
    per_point: dict[str, dict[str, object]] = {}
    for item in statistics:
        bucket = per_point.setdefault(
            str(item["point_id"]),
            {"rows": 0, "expected_hours": 0, "missing_hours": 0, "diff_rows": 0},
        )
        bucket["rows"] = int(bucket["rows"]) + int(item["rows"])
        bucket["expected_hours"] = int(bucket["expected_hours"]) + int(item["expected_hours"])
        bucket["missing_hours"] = int(bucket["missing_hours"]) + int(item["missing_hours"])
        bucket["diff_rows"] = int(bucket["diff_rows"]) + int(item["precipitation_rain_diff_rows"])

    return {
        "generated_at": datetime.now(tz=ZoneInfo(DEFAULT_TIMEZONE)).isoformat(),
        "root": str(root),
        "chunk_count": len(statistics),
        "total_rows": total_rows,
        "total_expected_hours": total_expected,
        "total_missing_hours": sum(int(item["missing_hours"]) for item in statistics),
        "total_duplicate_timestamps": sum(int(item["duplicate_timestamps"]) for item in statistics),
        "all_hour_aligned": all(bool(item["hour_aligned"]) for item in statistics),
        "all_offset_plus_08": all(bool(item["offset_always_plus_08"]) for item in statistics),
        "total_precipitation_rain_diff_rows": sum(
            int(item["precipitation_rain_diff_rows"]) for item in statistics
        ),
        "per_point": per_point,
        "chunks": statistics,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="run 目录（默认 data/raw）")
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--points", default="", help="逗号分隔的 point_id 子集，默认全部 5 个点")
    parser.add_argument(
        "--models",
        default=None,
        help="显式数据模型，作为 models 查询参数发送（不传则使用 API 默认 Best Match）",
    )
    parser.add_argument("--sleep-seconds", type=float, default=1.0, help="块间与重试的基准间隔")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--force", action="store_true", help="忽略已存在的小时表重新抓取")
    parser.add_argument("--summarize-only", action="store_true", help="只对已落盘小时表做质量统计")
    parser.add_argument(
        "--summary-path",
        type=Path,
        default=None,
        help="统计输出路径，默认 <root>/open-meteo/quality-summary.json",
    )
    return parser.parse_args(argv)


def _selected_points(raw: str) -> tuple[GridPoint, ...]:
    """Parse ``--points`` tolerating surrounding whitespace; empty means all points."""
    requested = [item.strip() for item in raw.split(",") if item.strip()]
    if not requested:
        return GRID_POINTS
    known = {point.point_id for point in GRID_POINTS}
    unknown = [item for item in requested if item not in known]
    if unknown:
        raise ValueError(
            f"unknown point_id(s): {', '.join(unknown)}; known: {', '.join(sorted(known))}"
        )
    return tuple(point for point in GRID_POINTS if point.point_id in requested)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = Path(args.root)
    try:
        selected = _selected_points(args.points)
        start_date = date.fromisoformat(args.start_date.strip())
        end_date = date.fromisoformat(args.end_date.strip())
    except ValueError as error:
        print(f"[error] {error}")
        return 2
    if args.max_retries < 1:
        print("[error] --max-retries must be at least 1")
        return 2
    if start_date > end_date:
        print(f"[error] --start-date must not be later than --end-date: {start_date} > {end_date}")
        return 2

    if args.summarize_only:
        summary = summarize(root, selected)
        target = args.summary_path or root / "open-meteo" / "quality-summary.json"
    else:
        summary = fetch_archive(
            root=root,
            start_date=start_date,
            end_date=end_date,
            points=selected,
            models=args.models,
            sleep_seconds=args.sleep_seconds,
            max_retries=args.max_retries,
            force=args.force,
        )
        target = args.summary_path or root / "open-meteo" / "fetch-summary.json"

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)[:4000])
    print(f"summary written to {target}")
    return 1 if (not args.summarize_only and (summary.get("failed") or summary.get("empty"))) else 0


if __name__ == "__main__":
    raise SystemExit(main())

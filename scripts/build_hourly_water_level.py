"""一次性端到端脚本：官方 cata_12720 文件 → 规范帧 → 1 小时序列 → 质量证据。

产物（全部落在 ``data/processed/``，被 ``.gitignore`` 忽略，不进入提交）：

- ``water_level_hourly_cata12720.csv``：小时序列表，**每行一小时**，列顺序为
  ``observed_at, water_level_m, is_imputed, quality_flag, source_record_id``。
  默认以 ``--primary-steps 0`` 生成，即**不做任何插补**，把插补阈值的选择权留给
  主 Agent 依据本脚本输出的空缺段分布裁定。
- ``hourly_interpolation_sensitivity.csv``：``max_interpolation_steps ∈ {0,1,2,3,6}``
  的敏感性表（插补行数、占比、剩余缺失桶、各 horizon 可用样本量）。
- ``hourly_quality_report.json``：完整机器可读证据（来源追溯、规范化统计、采样间隔
  分布、小时网格、空缺段分布、敏感性表、分段样本量、与 DS-1b 口径的交叉核对）。

用法::

    python scripts/build_hourly_water_level.py
    python scripts/build_hourly_water_level.py --primary-steps 2 --output data/processed/custom.csv
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from river_sentinel_ml.dataset import chronological_split
from river_sentinel_ml.ingestion.official_file import (
    DEFAULT_SOURCE_NAME,
    DEFAULT_SOURCE_URI,
    DEFAULT_TIMEZONE,
    build_hourly_water_level,
    describe_downloaded_file,
    inspect_official_archive,
    load_official_water_level_file,
    read_official_water_level_records,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = REPO_ROOT / "data" / "raw" / "wenzhou" / "cata_12720"
DEFAULT_OUTPUT_CSV = REPO_ROOT / "data" / "processed" / "water_level_hourly_cata12720.csv"
DEFAULT_SENSITIVITY_CSV = REPO_ROOT / "data" / "processed" / "hourly_interpolation_sensitivity.csv"
DEFAULT_REPORT_JSON = REPO_ROOT / "data" / "processed" / "hourly_quality_report.json"

OUTPUT_COLUMNS = ["observed_at", "water_level_m", "is_imputed", "quality_flag", "source_record_id"]
SENSITIVITY_STEPS = (0, 1, 2, 3, 6)
HORIZONS = (1, 3, 6)
DEFAULT_FREQUENCY = "1h"
DEFAULT_PRIMARY_STEPS = 0

#: §7 交叉核对目标：DS-1b 实测口径（不得为对齐而修改数据）。
DS1B_REFERENCE = {
    "hours_with_records": 2026,
    "missing_hours": 1064,
    "longest_gap_hours": 720,
    "days_with_records": 118,
}


@dataclass(frozen=True)
class GapSegment:
    """One continuous run of missing hourly buckets."""

    start_at: str
    end_at: str
    length: int


# --------------------------------------------------------------------------- #
# Evidence helpers
# --------------------------------------------------------------------------- #


def _resolve_input(explicit: str | None) -> Path:
    """Pick the input file: explicit argument wins, otherwise the newest raw copy."""
    if explicit:
        candidate = Path(explicit)
        if not candidate.exists():
            message = f"input file does not exist: {candidate}"
            raise FileNotFoundError(message)
        return candidate
    candidates = sorted(DEFAULT_INPUT_DIR.glob("*.csv"))
    if not candidates:
        message = f"no raw cata_12720 CSV found under {DEFAULT_INPUT_DIR}"
        raise FileNotFoundError(message)
    return candidates[-1]


def _sampling_interval_distribution(index: pd.DatetimeIndex) -> dict[str, float | None]:
    """Mode, median, min and max of the positive gaps between distinct timestamps."""
    unique = pd.DatetimeIndex(index).dropna().unique().sort_values()
    if len(unique) < 2:
        return {
            "mode_seconds": None,
            "median_seconds": None,
            "min_seconds": None,
            "max_seconds": None,
        }
    gaps = (unique[1:] - unique[:-1]).total_seconds().to_numpy()
    positive = gaps[gaps > 0]
    values, counts = np.unique(positive, return_counts=True)
    return {
        "mode_seconds": float(values[int(np.argmax(counts))]),
        "median_seconds": float(np.median(positive)),
        "min_seconds": float(positive.min()),
        "max_seconds": float(positive.max()),
    }


def _missing_runs(levels: pd.Series) -> list[tuple[int, int]]:
    """Return ``(start_index, end_index_exclusive)`` for each run of missing values."""
    missing = levels.isna().to_numpy()
    runs: list[tuple[int, int]] = []
    cursor = 0
    size = missing.size
    while cursor < size:
        if not missing[cursor]:
            cursor += 1
            continue
        start = cursor
        while cursor < size and missing[cursor]:
            cursor += 1
        runs.append((start, cursor))
    return runs


def _quantiles(lengths: list[int]) -> dict[str, float | None]:
    """p50 / p90 / p99 of run lengths, or ``None`` when there is no run."""
    if not lengths:
        return {"p50": None, "p90": None, "p99": None}
    values = np.array(lengths, dtype="float64")
    return {
        "p50": float(np.percentile(values, 50)),
        "p90": float(np.percentile(values, 90)),
        "p99": float(np.percentile(values, 99)),
    }


def _usable_samples(levels: pd.Series, horizons: tuple[int, ...]) -> dict[str, int]:
    """Count rows where both the input point and the target point carry a value.

    目标时刻没有观测的样本不可用（把 NaN 目标计入指标会失真），因此
    这里把「t 与 t+h 均为有限值」作为可用样本的定义，并在报告中说明。
    """
    valid = np.isfinite(levels.to_numpy(dtype="float64"))
    counts: dict[str, int] = {}
    for horizon in horizons:
        target = valid[horizon:]
        counts[f"h{horizon}"] = int(np.count_nonzero(valid[:-horizon] & target))
    return counts


def _day_statistics(frame: pd.DataFrame) -> dict[str, object]:
    """Days covered by observations, missing days and the longest missing-day streak.

    这是与 DS-1b「118 个有记录日 / 缺失 12 天」交叉核对的唯一口径：日期按
    ``Asia/Shanghai`` 本地日历日计算。
    """
    days = pd.DatetimeIndex(sorted({stamp.normalize() for stamp in frame["observed_at"]}))
    full_span = pd.date_range(days.min(), days.max(), freq="D", tz=days.tz)
    missing_days = full_span.difference(days)
    gaps = [(days[i + 1] - days[i]).days for i in range(len(days) - 1)]
    return {
        "days_with_records": len(days),
        "missing_days": len(missing_days),
        "missing_day_list": [stamp.strftime("%Y-%m-%d") for stamp in missing_days],
        "longest_missing_day_gap_days": max(gaps) if gaps else 0,
        "span_start": days.min().strftime("%Y-%m-%d"),
        "span_end": days.max().strftime("%Y-%m-%d"),
    }


def _timestamp_precision(rows: list[dict[str, object]]) -> dict[str, int]:
    """Count records whose official timestamp carries no clock time.

    DS-2b 实测发现极少数记录的时间只到日。它们会被 ``pandas`` 解析到当日 00:00，
    因此必须在报告中显式计数而不是悄悄当作精确读数使用。
    """
    values = [str(row.get("time_d") or "").strip() for row in rows]
    return {
        "date_only_records": sum(1 for value in values if value and " " not in value),
        "records_total": len(values),
    }


def _grid_statistics(hourly: pd.DataFrame) -> dict[str, object]:
    """Bucket counts, duplicates, non-finite values and the gap-length distribution."""
    levels = hourly["water_level_m"]
    expected = len(hourly)
    observed = int(levels.notna().sum())
    runs = _missing_runs(levels)
    lengths = sorted((end - start for start, end in runs), reverse=True)
    longest = max(((end - start, start, end) for start, end in runs), default=(0, 0, 0))
    longest_length, longest_start, longest_end = longest
    return {
        "first_at": hourly["observed_at"].iloc[0].isoformat() if expected else None,
        "last_at": hourly["observed_at"].iloc[-1].isoformat() if expected else None,
        "expected_buckets": expected,
        "observed_buckets": observed,
        "missing_buckets": expected - observed,
        "missing_rate": round(float((expected - observed) / expected), 6) if expected else None,
        "duplicate_timestamps": int(hourly["observed_at"].duplicated().sum()),
        "non_finite_values": int(np.count_nonzero(~np.isfinite(levels.to_numpy(dtype="float64")))),
        "gap_segments": len(runs),
        "gap_length_total_missing_hours": int(sum(lengths)),
        "gap_length_quantiles": _quantiles(lengths),
        "longest_gap_hours": int(longest_length),
        "longest_gap_start": (
            hourly["observed_at"].iloc[longest_start].isoformat() if longest_length else None
        ),
        "longest_gap_end": (
            hourly["observed_at"].iloc[longest_end - 1].isoformat() if longest_length else None
        ),
        "top_gaps": [
            asdict(
                GapSegment(
                    start_at=hourly["observed_at"].iloc[start].isoformat(),
                    end_at=hourly["observed_at"].iloc[end - 1].isoformat(),
                    length=int(end - start),
                )
            )
            for start, end in sorted(runs, key=lambda pair: pair[1] - pair[0], reverse=True)[:10]
        ],
    }


def _sensitivity_table(
    frame: pd.DataFrame,
    *,
    frequency: str,
    steps_candidates: tuple[int, ...],
) -> tuple[list[dict[str, object]], pd.DataFrame]:
    """Measure how each interpolation threshold changes the usable sample count."""
    table: list[dict[str, object]] = []
    grids: dict[int, pd.DataFrame] = {}
    for steps in steps_candidates:
        hourly = build_hourly_water_level(frame, frequency=frequency, max_interpolation_steps=steps)
        grids[steps] = hourly
        levels = hourly["water_level_m"]
        imputed = int(hourly["is_imputed"].sum())
        table.append(
            {
                "max_interpolation_steps": steps,
                "rows": len(hourly),
                "imputed_rows": imputed,
                "imputed_rate": round(imputed / len(hourly), 6) if len(hourly) else 0.0,
                "remaining_missing_buckets": int(levels.isna().sum()),
                **_usable_samples(levels, HORIZONS),
            }
        )
    return table, pd.DataFrame(table)


def _segment_statistics(hourly: pd.DataFrame) -> list[dict[str, object]]:
    """Per-split coverage and usable samples for the 0.70 / 0.15 / 0.15 partition."""
    split = chronological_split(hourly)
    segments: list[dict[str, object]] = []
    for name, part in (
        ("train", split.train),
        ("validation", split.validation),
        ("test", split.test),
    ):
        levels = part["water_level_m"]
        missing = int(levels.isna().sum())
        segments.append(
            {
                "segment": name,
                "start_at": part["observed_at"].iloc[0].isoformat(),
                "end_at": part["observed_at"].iloc[-1].isoformat(),
                "hours": len(part),
                "missing_buckets": missing,
                "missing_rate": round(missing / len(part), 6) if len(part) else None,
                **_usable_samples(levels, HORIZONS),
            }
        )
    return segments


def _write_hourly_csv(hourly: pd.DataFrame, destination: Path) -> None:
    """Write the hourly table with ISO 8601 timestamps including the UTC offset."""
    table = pd.DataFrame(
        {
            "observed_at": [stamp.isoformat() for stamp in hourly["observed_at"]],
            "water_level_m": hourly["water_level_m"].to_numpy(dtype="float64"),
            "is_imputed": hourly["is_imputed"].to_numpy(dtype=bool),
            "quality_flag": [
                "" if flag is None else str(flag) for flag in hourly["quality_flag"].tolist()
            ],
            "source_record_id": [
                "" if value is None else str(value) for value in hourly["source_record_id"].tolist()
            ],
        }
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(
        destination, index=False, columns=OUTPUT_COLUMNS, float_format="%.6f", lineterminator="\n"
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """Run the end-to-end build and print the key evidence numbers."""
    parser = argparse.ArgumentParser(
        description="Build the hourly cata_12720 series with quality evidence."
    )
    parser.add_argument(
        "--input", default=None, help="raw downloaded file (ZIP named *.csv by default)"
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_CSV), help="hourly CSV destination")
    parser.add_argument(
        "--sensitivity-output",
        default=str(DEFAULT_SENSITIVITY_CSV),
        help="sensitivity table destination",
    )
    parser.add_argument(
        "--report", default=str(DEFAULT_REPORT_JSON), help="JSON evidence destination"
    )
    parser.add_argument("--frequency", default=DEFAULT_FREQUENCY, help="resampling frequency alias")
    parser.add_argument(
        "--primary-steps",
        type=int,
        default=DEFAULT_PRIMARY_STEPS,
        help="interpolation threshold used for the written hourly table (0 = none)",
    )
    args = parser.parse_args(argv)

    source = _resolve_input(args.input)
    provenance = describe_downloaded_file(
        source, source_uri=DEFAULT_SOURCE_URI, source_name=DEFAULT_SOURCE_NAME
    )
    inspection = inspect_official_archive(source)

    raw_rows = read_official_water_level_records(source)
    frame, summary = load_official_water_level_file(
        source, source_uri=DEFAULT_SOURCE_URI, timezone=DEFAULT_TIMEZONE
    )
    hourly = build_hourly_water_level(
        frame, frequency=args.frequency, max_interpolation_steps=args.primary_steps
    )
    _write_hourly_csv(hourly, Path(args.output))

    sensitivity, sensitivity_frame = _sensitivity_table(
        frame, frequency=args.frequency, steps_candidates=SENSITIVITY_STEPS
    )
    sensitivity_frame.to_csv(Path(args.sensitivity_output), index=False, lineterminator="\n")

    grid = _grid_statistics(hourly)
    days = _day_statistics(frame)
    report = {
        "source_file": str(source),
        "provenance": asdict(provenance),
        "archive_inspection": asdict(inspection),
        "normalization": {
            "total_rows": summary.total_rows,
            "valid_rows": summary.valid_rows,
            "duplicate_rows": summary.duplicate_rows,
            "invalid_timestamp_rows": summary.invalid_timestamp_rows,
            "invalid_level_rows": summary.invalid_level_rows,
            "missing_level_rows": summary.missing_level_rows,
            "conservation_holds": (
                summary.total_rows
                == summary.valid_rows
                + summary.invalid_timestamp_rows
                + summary.invalid_level_rows
                + summary.missing_level_rows
                + summary.duplicate_rows
            ),
            "inferred_interval_minutes": summary.inferred_interval_minutes,
            "sampling_interval_seconds": _sampling_interval_distribution(
                pd.DatetimeIndex(frame["observed_at"])
            ),
            "timestamp_precision": _timestamp_precision(raw_rows),
        },
        "hourly_grid": {
            "frequency": args.frequency,
            "primary_max_interpolation_steps": args.primary_steps,
            "output_path": str(Path(args.output)),
            "rows": len(hourly),
            "first_at": hourly["observed_at"].iloc[0].isoformat(),
            "last_at": hourly["observed_at"].iloc[-1].isoformat(),
            **grid,
        },
        "calendar_days": days,
        "sensitivity": sensitivity,
        "split_segments": _segment_statistics(hourly),
        "ds1b_cross_check": {
            "reference": DS1B_REFERENCE,
            "measured": {
                "hours_with_records": grid["observed_buckets"],
                "missing_hours": grid["missing_buckets"],
                "longest_gap_hours": grid["longest_gap_hours"],
                "days_with_records": days["days_with_records"],
                "missing_days": days["missing_days"],
            },
        },
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    print("SOURCE_SHA256", provenance.sha256)
    print("SOURCE_BYTES", provenance.file_size_bytes)
    print(
        "RAW_ROWS",
        inspection.total_rows,
        "UNIQUE",
        inspection.unique_rows,
        "DUPES",
        inspection.duplicate_rows,
    )
    print(
        "NORMALIZED",
        summary.total_rows,
        "VALID",
        summary.valid_rows,
        "IV_TS",
        summary.invalid_timestamp_rows,
        "IV_LVL",
        summary.invalid_level_rows,
        "MISS_LVL",
        summary.missing_level_rows,
        "DUP",
        summary.duplicate_rows,
    )
    print("INTERVAL_SEC", report["normalization"]["sampling_interval_seconds"])
    print("PRECISION", report["normalization"]["timestamp_precision"])
    print("DAYS", days)
    print("HOURLY_ROWS", len(hourly), grid["first_at"], "->", grid["last_at"])
    print(
        "GRID",
        grid["expected_buckets"],
        "observed",
        grid["observed_buckets"],
        "missing",
        grid["missing_buckets"],
        "rate",
        grid["missing_rate"],
    )
    print(
        "GAPS",
        grid["gap_segments"],
        grid["gap_length_quantiles"],
        "longest",
        grid["longest_gap_hours"],
    )
    print("SENSITIVITY")
    print(sensitivity_frame.to_string(index=False))
    print("REPORT", report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""一次性端到端脚本：小时水位 + 小时降雨 → 无泄漏特征矩阵 → 机器可读证据。

输入（均为本地副本，原始数据不进 Git）：

- ``data/processed/water_level_hourly_cata12720.csv``：DS-2b 交付的小时水位表；
- ``data/raw/open-meteo/hourly/``：DS-3 交付的小时降雨 CSV 目录。

产物（全部落在 ``data/processed/``，被 ``.gitignore`` 忽略，不进入提交）：

- ``hourly_features_cata12720.csv``：严格因果的水雨对齐特征矩阵，列顺序由
  :func:`river_sentinel_ml.features.build_hourly_features` 冻结；
- ``hourly_features_manifest.json``：严格 JSON 证据（来源 SHA-256、行数、段边界、
  列清单、各 horizon 可用样本量、插补影响、降雨对齐、口径声明），**不含绝对路径**。

用法::

    python scripts/build_hourly_features.py
    python scripts/build_hourly_features.py --output data/processed/custom.csv
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from river_sentinel_ml.features import (
    DEFAULT_HORIZONS,
    TIMEZONE,
    build_hourly_features,
    feature_columns,
    load_hourly_rainfall_tables,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WATER_CSV = REPO_ROOT / "data" / "processed" / "water_level_hourly_cata12720.csv"
DEFAULT_RAIN_ROOT = REPO_ROOT / "data" / "raw" / "open-meteo" / "hourly"
DEFAULT_OUTPUT_CSV = REPO_ROOT / "data" / "processed" / "hourly_features_cata12720.csv"
DEFAULT_MANIFEST = REPO_ROOT / "data" / "processed" / "hourly_features_manifest.json"

HORIZONS: tuple[int, ...] = DEFAULT_HORIZONS
SEGMENT_ORDER: tuple[str, ...] = ("train", "validation", "test")

#: DS-2b 实测的全局可用样本口径（``t`` 与 ``t+h`` 均为有限值），用于对照说明差额。
DS2B_REFERENCE_USABLE: dict[str, int] = {"h1": 1955, "h3": 1807, "h6": 1613}

#: DS-3 实测的 ``rain_mm`` 与 ``precipitation_mm`` 原始行差异数，用于交叉核对。
DS3_REFERENCE_DIFF_ROWS = 1268

#: 口径声明（论文、页面、报告必须一致表述）。
CAVEATS: tuple[str, ...] = (
    "降雨口径：Open-Meteo Best Match 网格化再分析降雨，5 个网格点等权平均为"
    "「文成县区域降雨代理变量」；属历史再分析网格数据，不是水文测站的仪器观测值，"
    "也不包含任何面向未来的气象信息。",
    "有界前瞻（HS-D-5）：水位桶取桶内末次观测，行 t 可能包含 t 之后最多 1 小时的读数，"
    "因此 1 小时 horizon 的评估偏乐观；3/6 小时不受影响。",
    "严格因果：所有特征只依赖 <= t 的信息，段间不共享窗口，标签不跨段。",
    "插补：water_level_t 与滞后列保留插补值以便追溯，但目标时刻被插补的样本标记为"
    "usable_h{h}=False，不得参与训练与评估（C-5）。",
    "样本量有限、测试段有效小时偏少，所有结论一律为探索性/教学科研用途，"
    "不构成正式洪水预警，也不替代水行政主管部门的决策。",
)


# --------------------------------------------------------------------------- #
# Evidence helpers
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str:
    """Streaming SHA-256 of one file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _directory_digest(root: Path) -> str:
    """Order-independent digest of every CSV below ``root`` (path + per-file hash)."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.csv")):
        relative = path.relative_to(root).as_posix()
        digest.update(f"{relative}:{_sha256(path)}\n".encode("utf-8"))
    return digest.hexdigest().upper()


def _relative(path: Path) -> str:
    """Repository-relative POSIX path; manifests must never leak an absolute path."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def _read_water_csv(path: Path) -> pd.DataFrame:
    """Read the DS-2b hourly table, keeping the record identifiers as text."""
    frame = pd.read_csv(path, dtype={"source_record_id": "string"})
    stamps = pd.to_datetime(frame["observed_at"], format="ISO8601")
    frame["observed_at"] = stamps.dt.tz_convert(TIMEZONE)
    return frame


def _rainfall_row_count(root: Path) -> int:
    """Total data rows across every rainfall CSV (header excluded)."""
    total = 0
    for path in sorted(root.rglob("*.csv")):
        total += int(len(pd.read_csv(path, usecols=["observed_at"])))
    return total


def _precipitation_rain_diff_rows(root: Path) -> int:
    """Count raw rows where ``precipitation_mm`` and ``rain_mm`` disagree (DS-3 definition)."""
    differing = 0
    for path in sorted(root.rglob("*.csv")):
        frame = pd.read_csv(path)
        for column in ("precipitation_mm", "rain_mm"):
            if column not in frame.columns:
                raise ValueError(f"{path}: missing required column '{column}'")
        precipitation = frame["precipitation_mm"].astype("float64")
        rain = frame["rain_mm"].astype("float64")
        differs = ~(precipitation.eq(rain) | (precipitation.isna() & rain.isna()))
        differing += int(differs.sum())
    return differing


def _usability_ladder(water: pd.DataFrame, features: pd.DataFrame) -> dict[str, dict[str, int]]:
    """Break the usable-sample count down from the DS-2b口径 to the delivered contract.

    The ladder makes the contract visible instead of asserted: DS-2b counted ``t`` and
    ``t+h`` both finite over the whole grid; the delivered matrix additionally drops
    targets that were imputed and targets that fall outside their own segment.
    """
    levels = water["water_level_m"].to_numpy(dtype="float64")
    finite = np.isfinite(levels)
    imputed = water["is_imputed"].to_numpy(dtype=bool)
    segments = features["segment"].to_numpy(dtype=object)
    total = levels.size

    ladder: dict[str, dict[str, int]] = {}
    for horizon in HORIZONS:
        key = f"h{horizon}"
        base = int(np.count_nonzero(finite[:-horizon] & finite[horizon:]))
        target_present = int(
            np.count_nonzero(finite[:-horizon] & finite[horizon:] & ~imputed[horizon:])
        )
        same_segment = np.zeros(total - horizon, dtype=bool)
        for name in SEGMENT_ORDER:
            positions = np.flatnonzero(segments == name)
            if positions.size > horizon:
                same_segment[positions[0] : positions[-1] + 1 - horizon] = True
        delivered = int(
            np.count_nonzero(
                finite[:-horizon] & finite[horizon:] & ~imputed[horizon:] & same_segment
            )
        )
        measured = int(features[f"usable_{key}"].sum())
        ladder[key] = {
            "ds2b_reference_usable": DS2B_REFERENCE_USABLE[key],
            "recomputed_ds2b_usable": base,
            "after_dropping_imputed_targets": target_present,
            "after_requiring_target_in_segment": delivered,
            "measured_usable": measured,
        }
    return ladder


def _segment_evidence(features: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-segment row counts, coverage and usable sample counts."""
    evidence: list[dict[str, Any]] = []
    for name in SEGMENT_ORDER:
        part = features.loc[features["segment"] == name]
        entry: dict[str, Any] = {
            "segment": name,
            "start": part["observed_at"].iloc[0].isoformat(),
            "end": part["observed_at"].iloc[-1].isoformat(),
            "hours": int(len(part)),
            "missing_buckets": int(part["water_level_t"].isna().sum()),
        }
        for horizon in HORIZONS:
            entry[f"usable_h{horizon}"] = int(part[f"usable_h{horizon}"].sum())
        evidence.append(entry)
    return evidence


def _imputation_evidence(water: pd.DataFrame) -> dict[str, Any]:
    """Which rows were interpolated, and how many samples each horizon loses because of it."""
    imputed = water["is_imputed"].to_numpy(dtype=bool)
    positions = np.flatnonzero(imputed)
    lost: dict[str, int] = {}
    for horizon in HORIZONS:
        lost[f"h{horizon}"] = int(
            np.count_nonzero(imputed[horizon:] & np.isfinite(water["water_level_m"])[:-horizon])
        )
    return {
        "imputed_rows": int(positions.size),
        "imputed_at": [water["observed_at"].iloc[index].isoformat() for index in positions],
        "targets_dropped_per_horizon": lost,
        "policy": (
            "water_level_t 与滞后列保留插补值；目标时刻 is_imputed=True 的样本 usable_h{h}=False"
        ),
    }


def _rainfall_evidence(root: Path) -> dict[str, Any]:
    """Rainfall provenance, coverage and the two cross-checks against precipitation_mm."""
    primary = load_hourly_rainfall_tables(root)
    precipitation = load_hourly_rainfall_tables(root, value_column="precipitation_mm")
    differ = ~(
        primary["rainfall_mm"].eq(precipitation["rainfall_mm"])
        | (primary["rainfall_mm"].isna() & precipitation["rainfall_mm"].isna())
    )
    return {
        "relative_path": _relative(root),
        "csv_files": sum(1 for path in root.rglob("*.csv") if path.is_file()),
        "rows": _rainfall_row_count(root),
        "combined_sha256": _directory_digest(root),
        "value_column": "rain_mm",
        "points": sorted({path.parent.name for path in root.rglob("*.csv") if path.is_file()}),
        "hours": int(len(primary)),
        "first_at": primary["observed_at"].iloc[0].isoformat(),
        "last_at": primary["observed_at"].iloc[-1].isoformat(),
        "missing_grid_hours": int((primary["point_count"] == 0).sum()),
        "raw_rows_where_precipitation_differs_from_rain": _precipitation_rain_diff_rows(root),
        "hourly_mean_rows_where_precipitation_differs_from_rain": int(differ.sum()),
        "ds3_reference_raw_diff_rows": DS3_REFERENCE_DIFF_ROWS,
    }


def _json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats with ``null`` so the manifest stays valid JSON."""
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def _write_features(features: pd.DataFrame, destination: Path) -> None:
    """Write the feature matrix with ISO 8601 timestamps and fixed float precision."""
    table = features.copy()
    table["observed_at"] = [stamp.isoformat() for stamp in features["observed_at"]]
    destination.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(
        destination,
        index=False,
        columns=list(features.columns),
        float_format="%.6f",
        lineterminator="\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """Build the feature matrix, write the evidence manifest and print the key numbers."""
    parser = argparse.ArgumentParser(
        description="Build the leak-free hourly water-rainfall feature matrix."
    )
    parser.add_argument("--water", default=str(DEFAULT_WATER_CSV), help="hourly water CSV")
    parser.add_argument("--rain-root", default=str(DEFAULT_RAIN_ROOT), help="rainfall CSV root")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_CSV), help="feature CSV output")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="evidence JSON output")
    args = parser.parse_args(argv)

    water_path = Path(args.water)
    rain_root = Path(args.rain_root)
    output_path = Path(args.output)
    manifest_path = Path(args.manifest)
    if not water_path.is_file():
        raise FileNotFoundError(f"hourly water table does not exist: {water_path}")

    water = _read_water_csv(water_path)
    rainfall = load_hourly_rainfall_tables(rain_root)
    features = build_hourly_features(water, rainfall)
    _write_features(features, output_path)

    segments = _segment_evidence(features)
    ladder = _usability_ladder(water, features)
    manifest = {
        "task": "DS-4b",
        "dataset_id": "12720",
        "timezone": TIMEZONE,
        "sources": {
            "water_level_hourly": {
                "relative_path": _relative(water_path),
                "sha256": _sha256(water_path),
                "bytes": int(water_path.stat().st_size),
                "rows": int(len(water)),
                "first_at": water["observed_at"].iloc[0].isoformat(),
                "last_at": water["observed_at"].iloc[-1].isoformat(),
                "imputed_rows": int(water["is_imputed"].sum()),
            },
            "rainfall_directory": _rainfall_evidence(rain_root),
        },
        "output": {
            "relative_path": _relative(output_path),
            "sha256": _sha256(output_path),
            "bytes": int(output_path.stat().st_size),
            "rows": int(len(features)),
        },
        "columns": list(features.columns),
        "feature_columns": list(feature_columns(features)),
        "segments": segments,
        "usable_counts": {
            f"h{horizon}": int(features[f"usable_h{horizon}"].sum()) for horizon in HORIZONS
        },
        "usable_counts_by_segment": {
            f"h{horizon}": {entry["segment"]: entry[f"usable_h{horizon}"] for entry in segments}
            for horizon in HORIZONS
        },
        "usability_ladder": ladder,
        "imputation": _imputation_evidence(water),
        "rainfall_alignment": {
            "water_hours_without_rainfall": int(features["rain_point_count"].isna().sum()),
            "rain_point_count_distribution": {
                str(int(key)): int(value)
                for key, value in features["rain_point_count"]
                .value_counts(dropna=True)
                .sort_index()
                .items()
            },
        },
        "caveats": list(CAVEATS),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(_json_safe(manifest), ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    print("WATER", _sha256(water_path), len(water), water_path.stat().st_size)
    print("RAIN", manifest["sources"]["rainfall_directory"]["csv_files"], "files")
    print("RAIN_ROWS", manifest["sources"]["rainfall_directory"]["rows"])
    print(
        "RAIN_DIFF_ROWS",
        manifest["sources"]["rainfall_directory"]["raw_rows_where_precipitation_differs_from_rain"],
    )
    print("FEATURES", len(features), "rows", len(features.columns), "columns")
    print("SEGMENTS")
    print(pd.DataFrame(segments).to_string(index=False))
    print("USABLE")
    print(pd.DataFrame(ladder).T.to_string())
    print("IMPUTED", manifest["imputation"]["imputed_rows"], manifest["imputation"]["imputed_at"])
    print("RAIN_ALIGNMENT", manifest["rainfall_alignment"])
    print("OUTPUT", output_path, _sha256(output_path))
    print("MANIFEST", manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

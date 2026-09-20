"""Local file adapter for water-level records.

Supported inputs are ``.csv``, ``.json`` and ``.parquet``. Rows are returned in the
official field layout (``time_d``, ``up_water_level``, ``z_id``) without cleaning,
deduplication, imputation, unit conversion or timezone conversion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

SUPPORTED_SUFFIXES: tuple[str, ...] = (".csv", ".json", ".parquet")


def load_water_level_file(path: Path) -> list[dict[str, object]]:
    """Load raw water-level records from a local CSV, JSON or Parquet file."""
    suffix = Path(path).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(SUPPORTED_SUFFIXES)
        raise ValueError(
            f"unsupported file type {Path(path).suffix!r}, expected one of {supported}"
        )

    if suffix == ".csv":
        frame = pd.read_csv(path)
    elif suffix == ".json":
        frame = pd.DataFrame(_load_json_records(path))
    else:
        frame = pd.read_parquet(path)

    return [dict(row) for row in frame.to_dict(orient="records")]


def _load_json_records(path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} does not contain valid JSON: {exc}") from exc

    # 接口契约为 load_water_level_file 规定了 ValueError（见 ml/tests/ingestion/test_files.py），
    # 因此这里不使用 ruff 建议的 TypeError。
    if not isinstance(payload, list):
        raise ValueError(  # noqa: TRY004
            f"{path} must contain a JSON array of records, got {type(payload).__name__}"
        )
    for index, record in enumerate(payload):
        if not isinstance(record, dict):
            raise ValueError(  # noqa: TRY004
                f"record at index {index} in {path} must be an object, got {type(record).__name__}"
            )
    return payload

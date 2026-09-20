"""Tests for immutable raw snapshots and source traceability."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from river_sentinel_ml import provenance
from river_sentinel_ml.contracts import SourceManifest
from river_sentinel_ml.provenance import (
    SENSITIVE_PARAMETER_KEYS,
    SOURCE_NAMESPACE,
    SnapshotMetadata,
    write_raw_snapshot,
)

FIXED_ACQUIRED_AT = datetime(2026, 9, 20, 1, 30, 0, tzinfo=UTC)
DATASET_ID = "cata_12720"
SOURCE_URI = "https://data.wenzhou.gov.cn/jdop_front/detail/data.do?iid=12720"


def _fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the single acquisition clock so paths can be asserted exactly."""
    monkeypatch.setattr(provenance, "_acquired_now", lambda: FIXED_ACQUIRED_AT)


def _metadata(**overrides: object) -> SnapshotMetadata:
    params = overrides.pop("request_parameters", {"page": 1, "rows": 100})
    return SnapshotMetadata(
        source_name="温州市公共数据开放平台",
        source_uri=SOURCE_URI,
        dataset_id=DATASET_ID,
        request_parameters=params,
        **overrides,
    )


def _rows() -> list[dict[str, object]]:
    return [
        {"b": 2, "a": "文成县", "c": None},
        {
            "station_code": "330328001",
            "water_level_m": 12.34,
            "observed_at": "2026-09-20T01:00:00Z",
        },
    ]


def _reject_non_json_constant(name: str) -> object:
    """Strict-parser hook: NaN / Infinity / -Infinity must never reach a snapshot."""
    raise ValueError(f"non-JSON constant {name!r} in snapshot")


def _strict_parse_lines(text: str) -> list[object]:
    """Parse each NDJSON line with a strict parser that rejects non-JSON constants."""
    return [
        json.loads(line, parse_constant=_reject_non_json_constant)
        for line in text.splitlines()
        if line.strip()
    ]


def test_namespace_constants() -> None:
    assert SOURCE_NAMESPACE == "wenzhou"
    assert SENSITIVE_PARAMETER_KEYS == frozenset(
        {"appsecret", "token", "authorization", "password"}
    )


def test_ndjson_bytes_are_deterministic_sorted_and_unescaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)
    rows = [{"b": 2, "a": "文成县", "c": None}]

    snapshot_path, _, manifest = write_raw_snapshot(rows, _metadata(), tmp_path)

    payload = snapshot_path.read_bytes()
    text = payload.decode("utf-8")
    assert text == '{"a": "文成县", "b": 2, "c": null}\n'
    assert payload.endswith(b"\n")
    assert "\\u" not in text
    assert manifest.record_count == 1


def test_snapshot_path_layout_uses_utc_stamp_and_sha_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)

    snapshot_path, manifest_path, manifest = write_raw_snapshot(_rows(), _metadata(), tmp_path)

    expected_dir = tmp_path / "wenzhou" / DATASET_ID / "2026" / "09" / "20"
    expected_stem = f"20260920T013000Z-{manifest.sha256[:12]}"
    assert snapshot_path == expected_dir / f"{expected_stem}.ndjson"
    assert manifest_path == expected_dir / f"{expected_stem}.manifest.json"
    assert snapshot_path.exists()
    assert manifest_path.exists()
    assert len(manifest.sha256[:12]) == 12


def test_sha256_matches_exact_snapshot_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)

    snapshot_path, _, manifest = write_raw_snapshot(_rows(), _metadata(), tmp_path)

    assert manifest.sha256 == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", manifest.sha256)


def test_sensitive_parameters_are_removed_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)
    secret = "approved-appsecret-value-9f3c"
    metadata = _metadata(
        request_parameters={
            "appsecret": secret,
            "AppSecret": secret,
            "Authorization": "Bearer abc123",
            " Token ": "token-value",
            "password": "p@ssw0rd",
            "page": 1,
            "station_name": "文成",
        }
    )

    snapshot_path, manifest_path, manifest = write_raw_snapshot(_rows(), metadata, tmp_path)

    assert manifest.request_parameters == {"page": 1, "station_name": "文成"}
    for forbidden in (secret, "Bearer abc123", "token-value", "p@ssw0rd"):
        assert forbidden not in snapshot_path.read_text(encoding="utf-8")
        assert forbidden not in manifest_path.read_text(encoding="utf-8")


def test_existing_snapshot_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)
    first_snapshot, _, _ = write_raw_snapshot(_rows(), _metadata(), tmp_path)
    original_bytes = first_snapshot.read_bytes()

    with pytest.raises(FileExistsError):
        write_raw_snapshot(_rows(), _metadata(), tmp_path)

    assert first_snapshot.read_bytes() == original_bytes


def test_existing_manifest_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)
    snapshot_path, manifest_path, _ = write_raw_snapshot(_rows(), _metadata(), tmp_path)
    original_manifest_bytes = manifest_path.read_bytes()
    snapshot_path.unlink()

    with pytest.raises(FileExistsError):
        write_raw_snapshot(_rows(), _metadata(), tmp_path)

    assert manifest_path.read_bytes() == original_manifest_bytes
    assert not snapshot_path.exists()


def test_missing_manifest_does_not_recreate_a_bare_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)
    snapshot_path, manifest_path, _ = write_raw_snapshot(_rows(), _metadata(), tmp_path)
    original_snapshot_bytes = snapshot_path.read_bytes()
    manifest_path.unlink()

    with pytest.raises(FileExistsError):
        write_raw_snapshot(_rows(), _metadata(), tmp_path)

    assert snapshot_path.read_bytes() == original_snapshot_bytes
    assert not manifest_path.exists()


def test_manifest_write_failure_removes_the_bare_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)
    expected_dir = tmp_path / "wenzhou" / DATASET_ID / "2026" / "09" / "20"
    racing_content = '{"written_by": "another writer"}\n'
    injected = {"done": False}
    real_open = provenance.Path.open

    def racing_open(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        is_manifest_write = self.name.endswith(".manifest.json") and ("w" in mode or "x" in mode)
        if is_manifest_write and not injected["done"]:
            injected["done"] = True
            self.write_text(racing_content, encoding="utf-8")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(provenance.Path, "open", racing_open)

    with pytest.raises(FileExistsError):
        write_raw_snapshot(_rows(), _metadata(), tmp_path)

    assert not any(expected_dir.rglob("*.ndjson"))
    manifest_files = list(expected_dir.glob("*.manifest.json"))
    assert len(manifest_files) == 1
    assert manifest_files[0].read_text(encoding="utf-8") == racing_content


def test_acquired_at_is_timezone_aware_utc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fixed_now(monkeypatch)

    _, _, manifest = write_raw_snapshot(_rows(), _metadata(), tmp_path)

    assert manifest.acquired_at.utcoffset() == timedelta(0)
    assert manifest.acquired_at == FIXED_ACQUIRED_AT


def test_empty_rows_produce_empty_file_and_zero_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)

    snapshot_path, _, manifest = write_raw_snapshot([], _metadata(), tmp_path)

    assert snapshot_path.read_bytes() == b""
    assert manifest.record_count == 0
    assert manifest.sha256 == hashlib.sha256(b"").hexdigest()


def test_non_mapping_row_raises_type_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fixed_now(monkeypatch)

    with pytest.raises(TypeError):
        write_raw_snapshot([["station", "level"]], _metadata(), tmp_path)


@pytest.mark.parametrize("bad_value", [True, 1.5, None, ["a"]])
def test_invalid_parameter_value_raises_value_error(
    bad_value: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)
    metadata = _metadata(request_parameters={"page": bad_value})

    with pytest.raises(ValueError):
        write_raw_snapshot(_rows(), metadata, tmp_path)

    assert not any(tmp_path.rglob("*.ndjson"))


def test_nan_values_are_serialized_as_null(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fixed_now(monkeypatch)
    rows = [{"station_code": "330328001", "water_level_m": float("nan")}]

    snapshot_path, _, manifest = write_raw_snapshot(rows, _metadata(), tmp_path)

    text = snapshot_path.read_text(encoding="utf-8")
    assert "NaN" not in text
    assert text == '{"station_code": "330328001", "water_level_m": null}\n'
    assert _strict_parse_lines(text) == [{"station_code": "330328001", "water_level_m": None}]
    assert manifest.record_count == 1


def test_infinite_values_are_serialized_as_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)
    rows = [{"max_level": float("inf"), "min_level": float("-inf")}]

    snapshot_path, _, _ = write_raw_snapshot(rows, _metadata(), tmp_path)

    text = snapshot_path.read_text(encoding="utf-8")
    assert "Infinity" not in text
    assert "-Infinity" not in text
    assert text == '{"max_level": null, "min_level": null}\n'
    assert _strict_parse_lines(text) == [{"max_level": None, "min_level": None}]


def test_non_finite_values_in_nested_containers_are_nulled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)
    rows = [
        {
            "nested": {"x": float("nan")},
            "items": [1.5, float("inf")],
            "pair": (float("-inf"), "keep"),
        }
    ]

    snapshot_path, _, _ = write_raw_snapshot(rows, _metadata(), tmp_path)

    text = snapshot_path.read_text(encoding="utf-8")
    parsed = _strict_parse_lines(text)
    # 结构不变：键名、数组长度与顺序均与输入一致（顶层键名经 sort_keys 排序）
    assert parsed == [{"items": [1.5, None], "nested": {"x": None}, "pair": [None, "keep"]}]
    assert list(parsed[0]) == ["items", "nested", "pair"]
    assert parsed[0]["nested"]["x"] is None
    assert len(parsed[0]["items"]) == 2
    assert parsed[0]["items"][0] == 1.5
    assert parsed[0]["pair"][1] == "keep"
    assert "NaN" not in text
    assert "Infinity" not in text


def test_finite_rows_are_serialized_byte_identically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)
    rows = _rows()

    snapshot_path, _, _ = write_raw_snapshot(rows, _metadata(), tmp_path)

    expected = "".join(
        f"{json.dumps(dict(row), ensure_ascii=False, sort_keys=True)}\n" for row in rows
    )
    assert snapshot_path.read_bytes() == expected.encode("utf-8")


def test_non_finite_value_surviving_normalization_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """归一化被绕过时，allow_nan=False 必须抛错而不是写入非法 JSON。"""
    _fixed_now(monkeypatch)
    monkeypatch.setattr(provenance, "_replace_non_finite", lambda value: value)

    with pytest.raises(ValueError):
        write_raw_snapshot([{"water_level_m": float("nan")}], _metadata(), tmp_path)

    assert not any(tmp_path.rglob("*.ndjson"))


def test_manifest_file_matches_returned_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fixed_now(monkeypatch)

    _, manifest_path, manifest = write_raw_snapshot(_rows(), _metadata(), tmp_path)

    text = manifest_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert (
        text
        == json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
    )
    assert json.loads(text) == manifest.model_dump(mode="json")
    assert isinstance(manifest, SourceManifest)
    assert manifest.source_uri == SOURCE_URI
    assert manifest.dataset_id == DATASET_ID

"""端到端管线测试：样例文件审计与 API 审计的产物、契约与红线。

对应计划 Task 8 Step 1 与主 Agent 裁定 1/2/3/14 的测试要求。测试只通过公开入口驱动
:func:`river_sentinel_ml.pipeline.run_file_baseline` 与
:func:`river_sentinel_ml.pipeline.run_api_baseline`；API 路径一律使用
``httpx.MockTransport`` 或注入 client，**绝不发起真实网络请求**。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd
import pytest

from river_sentinel_ml import pipeline as pipeline_module
from river_sentinel_ml import provenance as provenance_module
from river_sentinel_ml.contracts import ExperimentManifest
from river_sentinel_ml.ingestion.wenzhou_api import MAX_PAGE_SIZE, WenzhouWaterLevelClient
from river_sentinel_ml.pipeline import run_api_baseline, run_file_baseline

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_PATH = REPO_ROOT / "data" / "samples" / "water_level_sample.csv"

#: 凭据相关字样：产物里既不能出现键名，更不能出现取值。
CREDENTIAL_MARKER = "appsecret"
API_SECRET = "unit-test-do-not-leak"

#: 样例口径（主 Agent 实测冻结）：12 行输入、9 行有效、推断间隔 60 分钟。
EXPECTED_SPLIT_SIZES = {"train": 8, "validation": 1, "test": 3}

#: 裁定 1.3：run 目录内的 5 类产物文件名严格保持第 6 节原名。
RUN_ARTIFACT_NAMES = (
    "processed.parquet",
    "quality-report.json",
    "baseline-metrics.json",
    "experiment-manifest.json",
    "baseline-report.md",
)
#: 裁定 14/R-2：run 目录自包含，快照 *.ndjson 与其 *.manifest.json 也在同一个 run 目录内。
SNAPSHOT_GLOB = "wenzhou/**/*.ndjson"
SNAPSHOT_MANIFEST_GLOB = "wenzhou/**/*.manifest.json"
#: 裁定 1.1/1.2：目录名固定 run-<YYYYmmddTHHMMSSZ>，同秒冲突追加序号。
RUN_DIR_PATTERN = r"run-\d{8}T\d{6}Z(-\d+)?"
#: 不含运行时间戳、跨运行应当完全一致的确定性产物。
DETERMINISTIC_ARTIFACTS = ("processed.parquet", "quality-report.json", "baseline-metrics.json")


def _reject_constant(literal: str) -> object:
    raise AssertionError(f"artifact contains the non-JSON literal {literal!r}")


def _load_json(path: Path) -> dict[str, object]:
    """严格解析产物 JSON：非 JSON 字面量与凭据字样都必须不存在。"""
    text = path.read_text(encoding="utf-8")
    assert CREDENTIAL_MARKER not in text.lower()
    assert API_SECRET not in text
    assert not re.search(r"\b(NaN|-?Infinity)\b", text)
    payload = json.loads(text, parse_constant=_reject_constant)
    assert isinstance(payload, dict)
    return payload


def _artifact_texts(result: object) -> list[str]:
    artifacts = result.artifacts  # type: ignore[attr-defined]
    texts = [
        artifacts.quality_report_path.read_text(encoding="utf-8"),
        artifacts.metrics_path.read_text(encoding="utf-8"),
        artifacts.experiment_manifest_path.read_text(encoding="utf-8"),
        artifacts.report_path.read_text(encoding="utf-8"),
        artifacts.manifest_path.read_text(encoding="utf-8"),
    ]
    processed = pd.read_parquet(artifacts.processed_path)
    texts.append(processed.to_json(orient="records", force_ascii=False))
    return texts


def _run_dirs(root: Path) -> list[Path]:
    return sorted(root.glob("run-*"))


def _write_station_csv(path: Path, codes: Sequence[str]) -> None:
    records = [
        {
            "time_d": f"2026-03-01 {hour:02d}:00:00",
            "up_water_level": 3.0 + hour / 10,
            "z_id": 3000 + hour,
            "station_code": codes[hour % len(codes)],
        }
        for hour in range(12)
    ]
    pd.DataFrame(records).to_csv(path, index=False)


def _write_invalid_timestamp_csv(path: Path) -> None:
    """12 行时间戳全部非法：规范化后为空帧，切分必然失败（裁定 3 注入点）。"""
    records = [
        {"time_d": "not-a-time", "up_water_level": 3.0 + hour / 10, "z_id": 5000 + hour}
        for hour in range(12)
    ]
    pd.DataFrame(records).to_csv(path, index=False)


def _api_rows(count: int) -> list[dict[str, object]]:
    return [
        {
            "time_d": f"2026-04-01 {hour:02d}:00:00",
            "up_water_level": 5.0 + hour / 10,
            "z_id": 4000 + hour,
        }
        for hour in range(count)
    ]


def _mock_transport(
    rows: Sequence[dict[str, object]], recorded: list[int] | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/get_total.do"):
            return httpx.Response(200, json={"status": 1, "data": len(rows)})
        if path.endswith("/get_data.do"):
            page_size = int(request.url.params.get("pageSize", str(MAX_PAGE_SIZE)))
            if recorded is not None:
                recorded.append(page_size)
            page_number = int(request.url.params.get("pageNum", "1"))
            start = (page_number - 1) * page_size
            return httpx.Response(
                200, json={"status": 1, "data": list(rows[start : start + page_size])}
            )
        raise AssertionError(f"unexpected request path: {path}")

    return httpx.MockTransport(handler)


def test_run_file_baseline_writes_every_artifact(tmp_path: Path) -> None:
    output_dir = tmp_path / "audit"
    result = run_file_baseline(input_path=SAMPLE_PATH, output_dir=output_dir)

    artifacts = result.artifacts
    for path in (
        artifacts.snapshot_path,
        artifacts.manifest_path,
        artifacts.processed_path,
        artifacts.quality_report_path,
        artifacts.metrics_path,
        artifacts.experiment_manifest_path,
        artifacts.report_path,
    ):
        assert path.exists(), path
    assert result.split_sizes == EXPECTED_SPLIT_SIZES

    # 裁定 14/R-2：run 目录 6 类产物 —— 原 5 类 + 快照 *.ndjson 与其 *.manifest.json。
    runs = _run_dirs(output_dir)
    assert len(runs) == 1
    run_dir = runs[0]
    assert artifacts.report_path.parent == run_dir
    for name in RUN_ARTIFACT_NAMES:
        assert (run_dir / name).exists(), name
    assert artifacts.snapshot_path.suffix == ".ndjson"
    assert artifacts.manifest_path.name.endswith(".manifest.json")
    # 快照仍由 provenance 的 <namespace>/<dataset_id>/YYYY/MM/DD/ 子层级决定，只是 root 换成 run 目录。
    parts = artifacts.snapshot_path.relative_to(run_dir).parts
    assert parts[0] == "wenzhou"
    assert len(parts) == 6, parts
    # 输出目录下不再有 run 之外的快照。
    assert list(output_dir.glob(SNAPSHOT_GLOB)) == []
    assert list(output_dir.glob(SNAPSHOT_MANIFEST_GLOB)) == []


def test_quality_report_matches_the_frozen_sample_audit(tmp_path: Path) -> None:
    result = run_file_baseline(input_path=SAMPLE_PATH, output_dir=tmp_path / "audit")

    payload = _load_json(result.artifacts.quality_report_path)
    quality = payload["quality"]
    assert isinstance(quality, dict)
    assert quality["total_rows"] == 12
    assert quality["valid_rows"] == 9
    assert quality["inferred_interval_minutes"] == 60.0
    assert quality["missing_level_rows"] == 1
    assert quality["invalid_timestamp_rows"] == 1
    assert quality["duplicate_rows"] == 1
    assert payload["frequency"] == "1h"
    assert result.quality.total_rows == 12
    assert result.quality.valid_rows == 9


def test_experiment_manifest_satisfies_the_contract(tmp_path: Path) -> None:
    result = run_file_baseline(input_path=SAMPLE_PATH, output_dir=tmp_path / "audit")

    payload = _load_json(result.artifacts.experiment_manifest_path)
    manifest = ExperimentManifest.model_validate(payload)
    assert manifest.status == "completed"
    assert manifest.failure_reason is None
    assert manifest.model_name == "persistence"
    assert manifest.feature_names == ("water_level_m",)
    assert manifest.horizons == (1, 3, 6)
    assert manifest.dataset_sha256 == result.source.sha256
    # 样例没有断面字段，按 D-4 记为 unknown 并在报告里显著标注。
    assert manifest.station_ids == ("unknown",)
    assert manifest.train_range[1] < manifest.validation_range[0]
    assert manifest.validation_range[1] < manifest.test_range[0]
    # 裁定 14/R-2：artifact_paths 全部相对 run 目录，且每条都真实存在。
    run_dir = result.artifacts.report_path.parent
    assert list(manifest.artifact_paths)[:5] == list(RUN_ARTIFACT_NAMES)
    assert manifest.artifact_paths[5:] == (
        result.artifacts.snapshot_path.relative_to(run_dir).as_posix(),
        result.artifacts.manifest_path.relative_to(run_dir).as_posix(),
    )
    for relative_path in manifest.artifact_paths:
        assert (run_dir / relative_path).exists(), relative_path
    assert manifest.parameters["run_id"] == run_dir.name


def test_metrics_report_lists_sample_counts_for_every_partition(tmp_path: Path) -> None:
    result = run_file_baseline(input_path=SAMPLE_PATH, output_dir=tmp_path / "audit")

    payload = _load_json(result.artifacts.metrics_path)
    partitions = payload["partitions"]
    assert isinstance(partitions, dict)
    assert set(partitions) == {"validation", "test"}

    for horizon in ("1", "3", "6"):
        validation = partitions["validation"]["horizons"][horizon]
        assert isinstance(validation, dict)
        # validation 段只有 1 行且该行是插补值，按 D-2/D-3 记为 0 样本并给出原因。
        assert validation["sample_count"] == 0
        assert validation["reason"] == "insufficient_samples"
        assert validation["mae"] is None

        test = partitions["test"]["horizons"][horizon]
        assert isinstance(test, dict)
        assert test["sample_count"] == 3
        assert test["reason"] is None
        assert test["mae"] is not None
        assert test["rmse"] is not None


def test_report_states_replay_step_and_look_ahead(tmp_path: Path) -> None:
    result = run_file_baseline(input_path=SAMPLE_PATH, output_dir=tmp_path / "audit")

    text = result.artifacts.report_path.read_text(encoding="utf-8")
    for phrase in ("历史回放", "步长", "桶内最后一次观测", "合成", "教学科研"):
        assert phrase in text, phrase


def test_artifacts_never_leak_credentials(tmp_path: Path) -> None:
    result = run_file_baseline(input_path=SAMPLE_PATH, output_dir=tmp_path / "audit")

    for text in _artifact_texts(result):
        assert API_SECRET not in text
        assert CREDENTIAL_MARKER not in text.lower()


def test_multiple_stations_without_a_selection_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "two-stations.csv"
    _write_station_csv(path, ("ST001", "ST002"))

    with pytest.raises(ValueError, match="--station-code"):
        run_file_baseline(input_path=path, output_dir=tmp_path / "audit")


def test_explicit_station_code_selects_one_station(tmp_path: Path) -> None:
    path = tmp_path / "two-stations.csv"
    _write_station_csv(path, ("ST001", "ST002"))

    result = run_file_baseline(input_path=path, output_dir=tmp_path / "audit", station_code="ST002")
    assert result.experiment.station_ids == ("ST002",)


def test_missing_output_directory_is_created(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper"
    result = run_file_baseline(input_path=SAMPLE_PATH, output_dir=target)

    assert target.is_dir()
    assert result.artifacts.report_path.exists()


def _freeze_acquisition(monkeypatch: pytest.MonkeyPatch, *stamps: datetime) -> None:
    """冻结快照获取时刻。

    ``provenance._acquired_now`` 的注释即声明“monkeypatch this to pin snapshot
    paths in tests”，因此用它固定时钟，禁止依赖 ``time.sleep``。
    """
    iterator: Iterator[datetime] = iter(stamps)
    monkeypatch.setattr(provenance_module, "_acquired_now", lambda: next(iterator))


def _freeze_run_clock(monkeypatch: pytest.MonkeyPatch, stamp: datetime) -> None:
    """冻结 pipeline 运行时钟，让两次运行确定性地落在同一秒。

    ``pipeline.datetime`` 在运行时只被 ``datetime.now(UTC)`` 使用（其余都是注解），
    冻结后 ``_new_run_dir`` 必然走同秒序号分支。
    """

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return stamp

    monkeypatch.setattr(pipeline_module, "datetime", _FrozenDateTime)


def test_two_consecutive_runs_without_sleep_both_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """裁定 14/R-3：同一 output_dir 不 sleep 连跑两次，两次都必须成功。

    冻结获取时刻后，两次运行的**时间戳与数据完全相同**（旧实现在该条件下必然
    因快照撞名抛 ``FileExistsError``）；现在两次都成功、各产生一个不同的 run
    目录、每个目录 6 类产物齐全，且首次产物逐字节不变。
    """
    output_dir = tmp_path / "audit"
    stamp = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
    _freeze_acquisition(monkeypatch, stamp, stamp)

    first = run_file_baseline(input_path=SAMPLE_PATH, output_dir=output_dir)
    first_dir = first.artifacts.report_path.parent
    first_bytes = {name: (first_dir / name).read_bytes() for name in RUN_ARTIFACT_NAMES}

    second = run_file_baseline(input_path=SAMPLE_PATH, output_dir=output_dir)
    second_dir = second.artifacts.report_path.parent

    assert first_dir != second_dir
    for directory in (first_dir, second_dir):
        assert re.fullmatch(RUN_DIR_PATTERN, directory.name), directory.name
        for name in RUN_ARTIFACT_NAMES:
            assert (directory / name).exists(), (directory, name)
        assert len(list(directory.glob(SNAPSHOT_GLOB))) == 1
        assert len(list(directory.glob(SNAPSHOT_MANIFEST_GLOB))) == 1

    # 同一秒内运行时第二个目录追加 -2；若跨秒则是另一个时间戳，两者都不覆盖。
    if second_dir.name.startswith(first_dir.name):
        assert second_dir.name == f"{first_dir.name}-2"

    # 首次产物在第二次运行后逐字节不变（不覆盖历史产物是硬要求）。
    assert {name: (first_dir / name).read_bytes() for name in RUN_ARTIFACT_NAMES} == first_bytes
    # 不含运行时间戳的产物本身具有确定性，跨运行也应逐字节一致。
    for name in DETERMINISTIC_ARTIFACTS:
        assert (second_dir / name).read_bytes() == first_bytes[name], name


def test_same_second_run_directory_gets_a_numeric_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """裁定 14/R-3：确定性地覆盖同秒分支 —— ``run-<stamp>-2`` 且两个目录都完整。"""
    output_dir = tmp_path / "audit"
    stamp = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
    _freeze_acquisition(monkeypatch, stamp, stamp)
    _freeze_run_clock(monkeypatch, stamp)

    first = run_file_baseline(input_path=SAMPLE_PATH, output_dir=output_dir)
    second = run_file_baseline(input_path=SAMPLE_PATH, output_dir=output_dir)

    assert first.artifacts.report_path.parent.name == "run-20260501T000000Z"
    assert second.artifacts.report_path.parent.name == "run-20260501T000000Z-2"
    for result in (first, second):
        run_dir = result.artifacts.report_path.parent
        assert result.split_sizes == EXPECTED_SPLIT_SIZES
        assert result.artifacts.snapshot_path.is_relative_to(run_dir)
        assert result.artifacts.manifest_path.is_relative_to(run_dir)


def test_failure_manifest_is_written_when_the_split_fails(tmp_path: Path) -> None:
    """裁定 3：切分之前失败落 failure-manifest.json，不伪造三段时间区间。"""
    path = tmp_path / "invalid-timestamps.csv"
    _write_invalid_timestamp_csv(path)
    output_dir = tmp_path / "audit"

    with pytest.raises(ValueError):
        run_file_baseline(input_path=path, output_dir=output_dir)

    runs = _run_dirs(output_dir)
    assert len(runs) == 1
    run_dir = runs[0]

    payload = _load_json(run_dir / "failure-manifest.json")
    assert payload["status"] == "failed"
    assert payload["failure_stage"] == "split"
    assert payload["failure_reason"]
    assert payload["run_id"] == run_dir.name
    # 裁定 14/R-2：raw_snapshot 是相对 run 目录的路径。
    snapshot_relative = str(payload["raw_snapshot"])
    assert snapshot_relative
    assert not Path(snapshot_relative).is_absolute()
    assert (run_dir / snapshot_relative).exists()
    assert payload["artifact_paths"] == []

    # 同目录不得出现实验清单、指标或 processed 半成品。
    assert not (run_dir / "experiment-manifest.json").exists()
    assert not (run_dir / "baseline-metrics.json").exists()
    assert not (run_dir / "processed.parquet").exists()

    text = (run_dir / "failure-manifest.json").read_text(encoding="utf-8")
    for forbidden in ("train_range", "validation_range", "test_range"):
        assert forbidden not in text


def test_missing_input_file_is_reported_in_chinese(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"[一-鿿]"):
        run_file_baseline(input_path=tmp_path / "missing.csv", output_dir=tmp_path / "audit")


def test_api_baseline_consumes_the_injected_client(tmp_path: Path) -> None:
    rows = _api_rows(12)
    client = WenzhouWaterLevelClient(API_SECRET, transport=_mock_transport(rows))

    result = run_api_baseline(output_dir=tmp_path / "api", client=client)

    assert result.source.record_count == 12
    assert result.split_sizes == EXPECTED_SPLIT_SIZES
    for text in _artifact_texts(result):
        assert API_SECRET not in text
        assert CREDENTIAL_MARKER not in text.lower()


def test_api_baseline_uses_the_default_page_size(tmp_path: Path) -> None:
    """裁定 2.5：默认路径必须把 MAX_PAGE_SIZE 透传给 client。"""
    recorded: list[int] = []
    client = WenzhouWaterLevelClient(API_SECRET, transport=_mock_transport(_api_rows(12), recorded))

    result = run_api_baseline(output_dir=tmp_path / "api", client=client)

    assert recorded == [MAX_PAGE_SIZE]
    assert result.experiment.parameters["page_size"] == MAX_PAGE_SIZE


def test_api_baseline_forwards_an_explicit_page_size(tmp_path: Path) -> None:
    recorded: list[int] = []
    client = WenzhouWaterLevelClient(API_SECRET, transport=_mock_transport(_api_rows(12), recorded))

    result = run_api_baseline(output_dir=tmp_path / "api", client=client, page_size=50)

    assert recorded == [50]
    assert result.experiment.parameters["page_size"] == 50


def test_api_baseline_without_credentials_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WENZHOU_DATA_APPSECRET", raising=False)

    with pytest.raises(ValueError, match=r"[一-鿿]"):
        run_api_baseline(output_dir=tmp_path / "api")

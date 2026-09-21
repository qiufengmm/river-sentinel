"""命令行入口测试：退出码、中文错误与凭据零回显。

对应计划 Task 8 Step 2 与主 Agent 裁定 1.6/1.7/2.5 的测试要求。
所有用例通过 ``main([...])`` 直接驱动，不依赖子进程、不发起真实网络请求。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from river_sentinel_ml import pipeline as pipeline_module
from river_sentinel_ml.cli import main
from river_sentinel_ml.ingestion.wenzhou_api import MAX_PAGE_SIZE

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_PATH = REPO_ROOT / "data" / "samples" / "water_level_sample.csv"

CREDENTIAL_MARKER = "appsecret"
API_SECRET = "unit-test-do-not-leak"

RUN_ARTIFACT_NAMES = (
    "processed.parquet",
    "quality-report.json",
    "baseline-metrics.json",
    "experiment-manifest.json",
    "baseline-report.md",
)


class _FakeClient:
    """记录分页大小的假客户端，用于验证 --page-size 一路透传。"""

    calls: list[int] = []

    def __init__(self, appsecret: str, transport: object = None) -> None:
        self.appsecret = appsecret

    def iter_rows(self, page_size: int = MAX_PAGE_SIZE):  # type: ignore[no-untyped-def]
        _FakeClient.calls.append(page_size)
        return iter(
            [
                {
                    "time_d": f"2026-04-01 {hour:02d}:00:00",
                    "up_water_level": 5.0 + hour / 10,
                    "z_id": 4000 + hour,
                }
                for hour in range(12)
            ]
        )


def _run_dirs(root: Path) -> list[Path]:
    return sorted(root.glob("run-*"))


def test_audit_file_returns_zero_and_writes_artifacts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "audit"

    code = main(["audit-file", "--input", str(SAMPLE_PATH), "--output-dir", str(output_dir)])

    assert code == 0
    runs = _run_dirs(output_dir)
    assert len(runs) == 1
    for name in RUN_ARTIFACT_NAMES:
        assert (runs[0] / name).exists(), name
    # 裁定 14/R-2：run 目录自包含 —— 5 类产物 + 快照 *.ndjson 与其 *.manifest.json。
    assert list(runs[0].glob("wenzhou/**/*.ndjson"))
    assert list(runs[0].glob("wenzhou/**/*.manifest.json"))
    assert not list(output_dir.glob("wenzhou/**/*.ndjson"))
    # 裁定 1.6：run 目录绝对路径打印到 stdout。
    assert str(runs[0].resolve()) in capsys.readouterr().out


def test_audit_file_reports_a_missing_input_in_chinese(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "audit-file",
            "--input",
            str(tmp_path / "missing.csv"),
            "--output-dir",
            str(tmp_path / "audit"),
        ]
    )

    assert code != 0
    assert re.search(r"[一-鿿]", capsys.readouterr().err)


def test_audit_api_without_credentials_fails_without_echoing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("WENZHOU_DATA_APPSECRET", raising=False)
    output_dir = tmp_path / "api"

    code = main(["audit-api", "--output-dir", str(output_dir)])

    captured = capsys.readouterr()
    assert code != 0
    assert "WENZHOU_DATA_APPSECRET" in captured.err
    assert re.search(r"[一-鿿]", captured.err)
    assert API_SECRET not in captured.err
    # 裁定 1.6：缺凭据时不得创建 run 目录，也不得留下任何产物。
    assert not output_dir.exists() or not list(output_dir.glob("**/processed.parquet"))
    assert not output_dir.exists() or _run_dirs(output_dir) == []


@pytest.mark.parametrize("value", ["0", str(MAX_PAGE_SIZE + 1), "abc"])
def test_audit_api_rejects_an_invalid_page_size(
    value: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """裁定 2.5：0 / 201 / abc 都非 0 退出并给出中文错误，不静默截断。"""
    code = main(["audit-api", "--output-dir", str(tmp_path / "api"), "--page-size", value])

    assert code != 0
    assert re.search(r"[一-鿿]", capsys.readouterr().err)


@pytest.mark.parametrize(("argument", "expected"), [(None, MAX_PAGE_SIZE), ("50", 50)])
def test_audit_api_forwards_page_size_to_the_client(
    argument: str | None,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WENZHOU_DATA_APPSECRET", API_SECRET)
    monkeypatch.setattr(pipeline_module, "WenzhouWaterLevelClient", _FakeClient)
    _FakeClient.calls = []
    argv = ["audit-api", "--output-dir", str(tmp_path / "api")]
    if argument is not None:
        argv += ["--page-size", argument]

    code = main(argv)

    assert code == 0
    assert _FakeClient.calls == [expected]


def test_unknown_subcommand_returns_nonzero() -> None:
    assert main(["not-a-command"]) != 0


def test_missing_required_argument_returns_nonzero() -> None:
    assert main(["audit-file"]) != 0

"""端到端编排：把一条原始记录流变成一份可复核的证据包。

流程（顺序固定，任一步失败都不留半成品）：

1. 读取源数据（本地文件，或已授权的官方接口）；
2. 写不可变原始快照与来源清单（含 SHA-256）；
3. 规范化字段并产出质量审计 :class:`QualitySummary`；
4. 按断面筛选 → 重采样成等间隔序列 → 按时间先后切分；
5. 在**完整序列**上生成持久性预测，再按段切片并剔除插补样本后评估；
6. 捕获当前 Git 提交；
7. 原子写出 processed Parquet、质量报告、指标、实验清单与人读报告。

两个入口（``run_file_baseline`` / ``run_api_baseline``）共用同一条内部编排，
只有「取原始行」的来源不同。函数本身不打印、不调用 ``sys.exit``，
退出码由 :mod:`river_sentinel_ml.cli` 负责。
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from river_sentinel_ml.baseline import persistence_forecast
from river_sentinel_ml.contracts import ExperimentManifest, QualitySummary, SourceManifest
from river_sentinel_ml.dataset import DatasetSplit, chronological_split, resample_water_level
from river_sentinel_ml.ingestion.files import load_water_level_file
from river_sentinel_ml.ingestion.wenzhou_api import MAX_PAGE_SIZE, WenzhouWaterLevelClient
from river_sentinel_ml.metrics import ForecastMetrics, evaluate_forecasts
from river_sentinel_ml.normalize import normalize_water_level_rows
from river_sentinel_ml.provenance import SnapshotMetadata, write_raw_snapshot
from river_sentinel_ml.reporting import (
    render_baseline_report,
    render_experiment_manifest,
    render_failure_manifest,
    render_metrics_report,
    render_quality_report,
)

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_FREQUENCY = "1h"
DEFAULT_STEPS = 2
DEFAULT_HORIZONS: tuple[int, ...] = (1, 3, 6)
DEFAULT_TRAIN_RATIO = 0.70
DEFAULT_VALIDATION_RATIO = 0.15
DEFAULT_DATASET_ID = "cata_12720"

MODEL_NAME = "persistence"
FEATURE_NAMES: tuple[str, ...] = ("water_level_m",)
RANDOM_SEED = 0
PARTITION_NAMES: tuple[str, ...] = ("train", "validation", "test")
#: 只有验证段与测试段参与指标报告：训练段用来拟合，不能同时当评估集。
EVALUATED_PARTITIONS: tuple[str, ...] = ("validation", "test")
INSUFFICIENT_SAMPLES_REASON = "insufficient_samples"
UNKNOWN_COMMIT = "unknown"

FILE_SOURCE_NAME = "wenzhou-water-level-file"
API_SOURCE_NAME = "wenzhou-water-level-api"
API_SOURCE_URI = "https://data.wenzhou.gov.cn/jdop_front/interfaces/cata_12720/get_data.do"

_CREDENTIAL_PATTERN = re.compile(r"(appsecret[=:]\s*)\S+", re.IGNORECASE)
_FAILURE_REASON_LIMIT = 300
_GIT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class BaselineArtifacts:
    """Paths of every artifact produced by one audit run."""

    snapshot_path: Path
    manifest_path: Path
    processed_path: Path
    quality_report_path: Path
    metrics_path: Path
    experiment_manifest_path: Path
    report_path: Path


@dataclass(frozen=True)
class BaselineResult:
    """Result of one successful audit run."""

    artifacts: BaselineArtifacts
    quality: QualitySummary
    source: SourceManifest
    experiment: ExperimentManifest
    split_sizes: dict[str, int]


def run_file_baseline(
    *,
    input_path: Path,
    output_dir: Path,
    timezone: str = DEFAULT_TIMEZONE,
    frequency: str = DEFAULT_FREQUENCY,
    max_interpolation_steps: int = DEFAULT_STEPS,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    train_ratio: float = DEFAULT_TRAIN_RATIO,
    validation_ratio: float = DEFAULT_VALIDATION_RATIO,
    station_code: str | None = None,
    dataset_id: str = DEFAULT_DATASET_ID,
) -> BaselineResult:
    """Run the full audit on a local ``.csv`` / ``.json`` / ``.parquet`` file.

    Raises:
        ValueError: 文件不存在、后缀不支持、内容无法解析、多断面未指定断面，
            或下游（重采样、切分）判定数据不足以出具结论。
    """
    path = Path(input_path)
    metadata = SnapshotMetadata(
        source_name=FILE_SOURCE_NAME,
        source_uri=path.resolve().as_uri(),
        dataset_id=dataset_id,
        request_parameters={"input_path": str(path.resolve()), "dataset_id": dataset_id},
    )

    def loader() -> list[dict[str, object]]:
        rows, _ = _load_rows_from_file(path)
        return rows

    return _orchestrate(
        loader=loader,
        metadata=metadata,
        output_dir=output_dir,
        timezone=timezone,
        frequency=frequency,
        max_interpolation_steps=max_interpolation_steps,
        horizons=horizons,
        train_ratio=train_ratio,
        validation_ratio=validation_ratio,
        station_code=station_code,
        source_kind="本地文件",
        source_label=f"本地文件 {path.resolve()}",
        sample_note=_file_sample_note(path),
    )


def run_api_baseline(
    *,
    output_dir: Path,
    appsecret: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
    frequency: str = DEFAULT_FREQUENCY,
    max_interpolation_steps: int = DEFAULT_STEPS,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    train_ratio: float = DEFAULT_TRAIN_RATIO,
    validation_ratio: float = DEFAULT_VALIDATION_RATIO,
    station_code: str | None = None,
    dataset_id: str = DEFAULT_DATASET_ID,
    page_size: int = MAX_PAGE_SIZE,
    client: WenzhouWaterLevelClient | None = None,
) -> BaselineResult:
    """Run the full audit on rows pulled from the Wenzhou water-level interface.

    ``client`` 仅供测试注入；未注入时才需要真实凭据。凭据缺失抛中文
    ``ValueError``，消息不含任何凭据取值。

    Note:
        ``page_size`` 是 CLI ``--page-size`` 需要的额外入参（Task 8 提示词 §5.1
        给出的冻结签名未包含它），经主 Agent 裁定 2 批准：keyword-only、默认取
        ``MAX_PAGE_SIZE``（从 ``wenzhou_api`` 导入，不硬编码 200），``client``
        仍保持在最后一位。取值沿用 ``iter_rows`` 自带的 ``1 <= page_size <= 200``
        校验，CLI 侧另做显式范围校验，绝不静默截断。
    """
    if client is None:
        secret = appsecret if appsecret is not None else _appsecret_from_environment()
        if not secret:
            raise ValueError(
                "缺少访问凭据：请设置环境变量 WENZHOU_DATA_APPSECRET 后重试"
                "（凭据不会写入任何产物、日志或异常消息）"
            )
        client = WenzhouWaterLevelClient(secret)
    metadata = SnapshotMetadata(
        source_name=API_SOURCE_NAME,
        source_uri=API_SOURCE_URI,
        dataset_id=dataset_id,
        request_parameters={"dataset_id": dataset_id, "page_size": int(page_size)},
    )

    def loader() -> list[dict[str, object]]:
        return list(client.iter_rows(page_size=page_size))

    return _orchestrate(
        loader=loader,
        metadata=metadata,
        output_dir=output_dir,
        timezone=timezone,
        frequency=frequency,
        max_interpolation_steps=max_interpolation_steps,
        horizons=horizons,
        train_ratio=train_ratio,
        validation_ratio=validation_ratio,
        station_code=station_code,
        source_kind="官方接口",
        source_label=f"温州市公共数据开放平台接口 {API_SOURCE_URI}",
        sample_note="本次为接口回放下的数据，结论受接口审批状态与更新时间影响。",
        page_size=page_size,
    )


# --------------------------------------------------------------------------- #
# 内部编排
# --------------------------------------------------------------------------- #


def _orchestrate(
    *,
    loader: Callable[[], Sequence[Mapping[str, object]]],
    metadata: SnapshotMetadata,
    output_dir: Path,
    timezone: str,
    frequency: str,
    max_interpolation_steps: int,
    horizons: tuple[int, ...],
    train_ratio: float,
    validation_ratio: float,
    station_code: str | None,
    source_kind: str,
    source_label: str,
    sample_note: str,
    page_size: int | None = None,
) -> BaselineResult:
    started_at = datetime.now(UTC)
    root = Path(output_dir)
    # 裁定 1：run 目录名固定 run-<YYYYmmddTHHMMSSZ>，同秒冲突追加序号，绝不覆盖。
    run_dir = _new_run_dir(root, started_at)
    snapshot_path: Path | None = None
    manifest_path: Path | None = None
    source: SourceManifest | None = None

    steps = tuple(int(horizon) for horizon in horizons)
    processed_path = run_dir / "processed.parquet"
    quality_report_path = run_dir / "quality-report.json"
    metrics_path = run_dir / "baseline-metrics.json"
    experiment_manifest_path = run_dir / "experiment-manifest.json"
    failure_manifest_path = run_dir / "failure-manifest.json"
    report_path = run_dir / "baseline-report.md"

    written: list[Path] = []
    ranges: dict[str, tuple[datetime, datetime]] | None = None
    split_sizes: dict[str, int] | None = None
    parameters = _parameters(
        source_kind=source_kind,
        timezone=timezone,
        frequency=frequency,
        horizons=steps,
        max_interpolation_steps=max_interpolation_steps,
        train_ratio=train_ratio,
        validation_ratio=validation_ratio,
        run_id=run_dir.name,
        page_size=page_size,
    )
    environment = _environment()
    code_commit, commit_note = _capture_code_commit([Path.cwd(), Path(__file__).resolve().parent])

    stage = "fetch"
    try:
        rows = loader()
        # 裁定 14/R-1（修订裁定 1.4）：原始快照写进当次 run 目录。
        # run 目录名唯一（时间戳 + 同秒序号 -2），快照路径随之唯一，永不撞名；
        # provenance.py 与 §3 的 <namespace>/<dataset_id>/YYYY/MM/DD/ 子层级保持冻结不变。
        snapshot_path, manifest_path, source = write_raw_snapshot(rows, metadata, run_dir)
        experiment_id = f"persistence-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{source.sha256[:8]}"
        stage = "normalize"
        frame, quality = normalize_water_level_rows(rows, timezone)
        selected, resolved_station, station_note = _select_station(frame, station_code)
        stage = "resample"
        resampled = resample_water_level(
            selected, frequency=frequency, max_interpolation_steps=max_interpolation_steps
        )
        stage = "split"
        split = chronological_split(
            resampled, train_ratio=train_ratio, validation_ratio=validation_ratio
        )
        split_sizes = {
            "train": len(split.train),
            "validation": len(split.validation),
            "test": len(split.test),
        }
        ranges = {name: _range_of(part) for name, part in _partitions(split).items()}
        parameters["station_code"] = resolved_station

        # D-1：先在完整序列上生成预测，再按段切片；段内独立 shift 会产生全 NaN。
        forecast = persistence_forecast(_levels_series(resampled), steps)
        metrics_by_partition, reasons, partition_counts = _evaluate_partitions(
            resampled=resampled, split_sizes=split_sizes, forecast=forecast, horizons=steps
        )
        gap_note = _gap_note(resampled, frequency, max_interpolation_steps)

        _write_parquet(resampled, processed_path)
        written.append(processed_path)
        _write_text(
            quality_report_path,
            render_quality_report(
                quality,
                imputed_by_partition=partition_counts,
                frequency=frequency,
                station_code=resolved_station,
                gap_note=gap_note,
            ),
        )
        written.append(quality_report_path)
        _write_text(
            metrics_path,
            render_metrics_report(metrics_by_partition, horizons=steps, reasons=reasons),
        )
        written.append(metrics_path)

        finished_at = datetime.now(UTC)
        # 裁定 14/R-2：run 目录自包含，artifact_paths 填相对 run 目录的产物路径
        # （5 类产物文件名不变 + 快照 *.ndjson 与其 *.manifest.json）。
        artifact_paths = (
            processed_path.name,
            quality_report_path.name,
            metrics_path.name,
            experiment_manifest_path.name,
            report_path.name,
            _relative(snapshot_path, run_dir),
            _relative(manifest_path, run_dir),
        )
        experiment = ExperimentManifest(
            experiment_id=experiment_id,
            created_at=started_at,
            dataset_sha256=source.sha256,
            horizons=steps,
            train_range=ranges["train"],
            validation_range=ranges["validation"],
            test_range=ranges["test"],
            code_commit=code_commit,
            station_ids=(resolved_station,) if resolved_station else ("unknown",),
            feature_names=FEATURE_NAMES,
            model_name=MODEL_NAME,
            parameters=parameters,
            random_seed=RANDOM_SEED,
            environment=environment,
            started_at=started_at,
            finished_at=finished_at,
            artifact_paths=artifact_paths,
            status="completed",
        )
        _write_text(experiment_manifest_path, render_experiment_manifest(experiment))
        written.append(experiment_manifest_path)

        digests = {
            "processed.parquet": _digest(processed_path),
            "quality-report.json": _digest(quality_report_path),
            "baseline-metrics.json": _digest(metrics_path),
            "experiment-manifest.json": _digest(experiment_manifest_path),
            "baseline-report.md": "本文件（自引用，写盘后另行校验）",
        }
        artifacts = {
            "processed.parquet": processed_path.name,
            "quality-report.json": quality_report_path.name,
            "baseline-metrics.json": metrics_path.name,
            "experiment-manifest.json": experiment_manifest_path.name,
            "baseline-report.md": report_path.name,
            "raw-snapshot": _relative(snapshot_path, run_dir),
            "source-manifest": _relative(manifest_path, run_dir),
        }
        _write_text(
            report_path,
            render_baseline_report(
                generated_at=finished_at,
                source=source,
                source_kind=source_kind,
                source_label=source_label,
                quality=quality,
                frequency=frequency,
                station_code=resolved_station,
                station_note=station_note,
                split_sizes=split_sizes,
                ranges=ranges,
                imputed_by_partition=partition_counts,
                metrics_by_partition=metrics_by_partition,
                reasons=reasons,
                horizons=steps,
                parameters=parameters,
                environment=environment,
                code_commit=code_commit,
                commit_note=commit_note,
                artifacts=artifacts,
                artifact_digests=digests,
                gap_note=gap_note,
                sample_note=sample_note,
                status_note="completed（全部产物已完整写出）",
            ),
        )
        written.append(report_path)
    except Exception as error:
        # D-7：保留原始快照与来源清单，删除本次生成的半成品 processed / metrics 文件。
        _discard(written)
        reason = _without_credentials(f"{type(error).__name__}: {error}")[:_FAILURE_REASON_LIMIT]
        if ranges is None:
            # 裁定 3：切分之前失败，区间未知 → 落 failure-manifest.json，
            # 不构造 ExperimentManifest（区间是非 Optional 字段，占位即伪造）。
            _write_text(
                failure_manifest_path,
                render_failure_manifest(
                    stage=stage,
                    reason=reason,
                    run_id=run_dir.name,
                    code_commit=code_commit,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    artifact_paths=(),
                    raw_snapshot=(
                        None if snapshot_path is None else _relative(snapshot_path, run_dir)
                    ),
                ),
            )
        elif source is not None:
            # 切分之后的失败：区间已知 → 仍按 D-7 落 status="failed" 的实验清单。
            _write_failed_experiment_manifest(
                path=experiment_manifest_path,
                experiment_id=experiment_id,
                source=source,
                started_at=started_at,
                horizons=steps,
                ranges=ranges,
                station_id=resolved_station,
                parameters=parameters,
                environment=environment,
                artifact_paths=(
                    _relative(snapshot_path, run_dir),
                    _relative(manifest_path, run_dir),
                ),
                reason=reason,
                code_commit=code_commit,
            )
        raise

    return BaselineResult(
        artifacts=BaselineArtifacts(
            snapshot_path=snapshot_path,
            manifest_path=manifest_path,
            processed_path=processed_path,
            quality_report_path=quality_report_path,
            metrics_path=metrics_path,
            experiment_manifest_path=experiment_manifest_path,
            report_path=report_path,
        ),
        quality=quality,
        source=source,
        experiment=experiment,
        split_sizes=split_sizes,
    )


# --------------------------------------------------------------------------- #
# 数据来源
# --------------------------------------------------------------------------- #


def _load_rows_from_file(path: Path) -> tuple[list[dict[str, object]], str]:
    """读取本地源文件，把 pandas / OS 异常转成中文 ``ValueError``。"""
    try:
        rows = load_water_level_file(path)
    except FileNotFoundError as error:
        raise ValueError(f"源数据文件不存在：{path}") from error
    except (OSError, ValueError) as error:
        raise ValueError(f"无法读取源数据文件 {path}：{error}") from error
    return rows, path.resolve().as_uri()


def _appsecret_from_environment() -> str | None:
    """从配置里取接口凭据；延迟导入，避免模块导入时读取环境变量。"""
    from river_sentinel_ml.config import Settings  # noqa: PLC0415

    secret = Settings().wenzhou_data_appsecret
    return None if secret is None else secret.get_secret_value()


# --------------------------------------------------------------------------- #
# 断面、序列与评估
# --------------------------------------------------------------------------- #


def _select_station(
    frame: pd.DataFrame, station_code: str | None
) -> tuple[pd.DataFrame, str | None, str]:
    """按断面筛选（D-4），返回（筛选后的帧、使用的断面、报告用说明）。"""
    if station_code is not None:
        selected = frame[frame["station_code"] == station_code]
        if selected.empty:
            raise ValueError(
                f"源数据中没有断面 {station_code!r} 的记录，请检查 --station-code 的取值"
            )
        return (
            selected.reset_index(drop=True),
            station_code,
            f"按参数显式筛选断面 {station_code}。",
        )

    codes: list[str] = []
    if "station_code" in frame.columns:
        codes = [str(code) for code in frame["station_code"].dropna().unique()]
    if not codes:
        return (
            frame,
            None,
            "源数据未提供断面标识，`station_ids` 记为 `unknown`；结论只对该批记录整体成立。",
        )
    if len(codes) == 1:
        return frame, codes[0], f"源数据只包含一个断面 {codes[0]}，直接采用。"
    listed = ", ".join(codes[:5])
    suffix = ", ..." if len(codes) > 5 else ""
    raise ValueError(
        f"源数据包含多个断面（station_code：{listed}{suffix}），"
        "请用 --station-code 指定其中一个后再重跑"
    )


def _levels_series(frame: pd.DataFrame) -> pd.Series:
    """把重采样帧转成以观测时间为索引的水位序列。"""
    return pd.Series(
        frame["water_level_m"].to_numpy(dtype="float64"),
        index=pd.DatetimeIndex(frame["observed_at"]),
        name="water_level_m",
    )


def _partitions(split: DatasetSplit) -> dict[str, pd.DataFrame]:
    return {"train": split.train, "validation": split.validation, "test": split.test}


def _range_of(part: pd.DataFrame) -> tuple[datetime, datetime]:
    stamps = pd.DatetimeIndex(part["observed_at"])
    return stamps.min().to_pydatetime(), stamps.max().to_pydatetime()


def _evaluate_partitions(
    *,
    resampled: pd.DataFrame,
    split_sizes: Mapping[str, int],
    forecast: pd.DataFrame,
    horizons: tuple[int, ...],
) -> tuple[
    dict[str, dict[int, ForecastMetrics | None]],
    dict[str, dict[int, str]],
    dict[str, dict[str, int]],
]:
    """按段切片、剔除插补与缺失样本（D-3），再逐段逐步长评估（D-2）。"""
    offsets: dict[str, tuple[int, int]] = {}
    cursor = 0
    for name in PARTITION_NAMES:
        size = int(split_sizes[name])
        offsets[name] = (cursor, cursor + size)
        cursor += size

    metrics_by_partition: dict[str, dict[int, ForecastMetrics | None]] = {}
    reasons: dict[str, dict[int, str]] = {}
    counts: dict[str, dict[str, int]] = {}

    for name in PARTITION_NAMES:
        start, end = offsets[name]
        part = resampled.iloc[start:end]
        imputed = part["is_imputed"].astype(bool).to_numpy()
        missing = part["water_level_m"].isna().to_numpy()
        counts[name] = {
            "rows": int(len(part)),
            "imputed": int(imputed.sum()),
            "missing": int(missing.sum()),
            "evaluated": 0,
        }
        if name not in EVALUATED_PARTITIONS:
            continue

        keep = ~imputed & ~missing
        counts[name]["evaluated"] = int(keep.sum())
        window = forecast.iloc[start:end].reset_index(drop=True).loc[keep]

        partition_metrics: dict[int, ForecastMetrics | None] = {}
        partition_reasons: dict[int, str] = {}
        for horizon in horizons:
            column = f"prediction_h{horizon}"
            try:
                evaluated = evaluate_forecasts(window[["observed", column]], (horizon,))
            except ValueError:
                partition_metrics[horizon] = None
                partition_reasons[horizon] = INSUFFICIENT_SAMPLES_REASON
                continue
            partition_metrics[horizon] = evaluated[horizon]
        metrics_by_partition[name] = partition_metrics
        reasons[name] = partition_reasons

    return metrics_by_partition, reasons, counts


def _gap_note(frame: pd.DataFrame, frequency: str, steps: int) -> str:
    flags = frame["is_imputed"].astype(bool).to_numpy() | frame["water_level_m"].isna().to_numpy()
    imputed_total = int(frame["is_imputed"].astype(bool).sum())
    missing_total = int(frame["water_level_m"].isna().sum())
    return (
        f"重采样后共 {len(frame)} 个网格点（频率 {frequency}）：插补 {imputed_total} 行、"
        f"仍缺失 {missing_total} 行，最长连续缺口 {_longest_run(flags)} 步；"
        f"插补阈值 max_interpolation_steps={steps}，超过阈值的长缺口保持缺失且不参与评估。"
    )


def _longest_run(flags: np.ndarray) -> int:
    longest = 0
    current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest


# --------------------------------------------------------------------------- #
# 参数、环境与代码版本
# --------------------------------------------------------------------------- #


def _parameters(
    *,
    source_kind: str,
    timezone: str,
    frequency: str,
    horizons: tuple[int, ...],
    max_interpolation_steps: int,
    train_ratio: float,
    validation_ratio: float,
    run_id: str,
    page_size: int | None = None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "model_name": MODEL_NAME,
        "run_id": run_id,
        "source_kind": source_kind,
        "timezone": timezone,
        "frequency": frequency,
        "horizons": [int(horizon) for horizon in horizons],
        "max_interpolation_steps": int(max_interpolation_steps),
        "train_ratio": float(train_ratio),
        "validation_ratio": float(validation_ratio),
        "station_code": None,
        "random_seed_note": "持久性基线不含随机性，random_seed 固定为 0",
    }
    if page_size is not None:
        # 裁定 2.4：接口分页大小属于可复现性参数，必须记录。
        parameters["page_size"] = int(page_size)
    return parameters


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pandas": pd.__version__,
        "numpy": np.__version__,
    }


def _capture_code_commit(candidates: Sequence[Path]) -> tuple[str, str]:
    """用 ``git rev-parse HEAD`` 捕获提交号；失败时如实写 ``unknown``（D-9）。"""
    for base in candidates:
        try:
            completed = subprocess.run(  # noqa: S603
                ["git", "rev-parse", "HEAD"],
                cwd=base,
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip(), ""
    return (
        UNKNOWN_COMMIT,
        "当前环境无法执行 git rev-parse HEAD（非 Git 工作树或 git 不可用），如实记为 unknown。",
    )


# --------------------------------------------------------------------------- #
# 原子写入、清理与失败清单
# --------------------------------------------------------------------------- #


def _new_run_dir(root: Path, started_at: datetime) -> Path:
    """在输出目录下新建本次运行的子目录，绝不覆盖历史产物。

    目录名固定 ``run-<YYYYmmddTHHMMSSZ>``（UTC、秒级、Windows 合法）；
    同一秒内再次运行时追加 ``-2``、``-3``…，始终新建、绝不覆盖。
    """
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    candidate = root / f"run-{stamp}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = root / f"run-{stamp}-{suffix}"
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _write_text(path: Path, text: str) -> None:
    """同目录临时文件 + ``replace`` 的原子写入。"""
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _write_parquet(frame: pd.DataFrame, path: Path) -> None:
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    frame.to_parquet(temporary, index=False)
    temporary.replace(path)


def _discard(paths: Iterable[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _without_credentials(text: str) -> str:
    """兜底脱敏：任何 ``appsecret=xxx`` 形态的取值都不进入产物。"""
    return _CREDENTIAL_PATTERN.sub(r"\1<redacted>", text)


def _write_failed_experiment_manifest(
    *,
    path: Path,
    experiment_id: str,
    source: SourceManifest,
    started_at: datetime,
    horizons: tuple[int, ...],
    ranges: Mapping[str, tuple[datetime, datetime]],
    station_id: str | None,
    parameters: Mapping[str, Any],
    environment: Mapping[str, str],
    artifact_paths: tuple[str, ...],
    reason: str,
    code_commit: str,
) -> None:
    """**切分之后**失败时落盘 ``status="failed"`` 的实验清单（裁定 3）。

    切分之前失败由 ``render_failure_manifest`` 处理：那时三段区间未知，
    而它们在契约里是非 Optional 字段，占位即伪造。
    """
    manifest = ExperimentManifest(
        experiment_id=experiment_id,
        created_at=started_at,
        dataset_sha256=source.sha256,
        horizons=horizons,
        train_range=ranges["train"],
        validation_range=ranges["validation"],
        test_range=ranges["test"],
        code_commit=code_commit,
        station_ids=(station_id,) if station_id else ("unknown",),
        feature_names=FEATURE_NAMES,
        model_name=MODEL_NAME,
        parameters=dict(parameters),
        random_seed=RANDOM_SEED,
        environment=dict(environment),
        started_at=started_at,
        finished_at=datetime.now(UTC),
        artifact_paths=artifact_paths,
        status="failed",
        failure_reason=reason[:_FAILURE_REASON_LIMIT],
    )
    _write_text(path, render_experiment_manifest(manifest))


def _file_sample_note(path: Path) -> str:
    if "samples" in path.parts:
        return "本次输入是仓库内的合成样例数据（非真实观测值），结论只用于验证管线。"
    return "本次输入为本地历史文件，属于历史回放 / 离线数据，不代表实时水情。"

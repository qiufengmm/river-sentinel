"""把审计结果渲染成可复核的 JSON 与 Markdown 产物。

本模块只做渲染：不读文件、不访问网络、不写盘，也不做任何统计推断。
所有 JSON 都经过 ``allow_nan=False`` 与 ``ensure_ascii=False``，
保证产物是 RFC 8259 合法 JSON（缺失指标一律写 ``null``，绝不写 ``NaN``）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from river_sentinel_ml.contracts import ExperimentManifest, QualitySummary, SourceManifest
from river_sentinel_ml.metrics import ForecastMetrics

#: 预测时域口径（跨任务决策 D-6）：在采样频率被证实为小时之前只称“步长”。
STEP_UNIT_NOTE = (
    "报告中的 1 / 3 / 6 指预测**步长**（网格步数），不是小时；"
    "只有当选用的采样频率被证实为小时时，才可以在下游与论文中改称小时。"
)

#: 有界前瞻声明（跨任务决策 D-5 / 约束 C-6）。
BOUNDED_LOOK_AHEAD_NOTE = (
    "重采样时每个网格点取**桶内最后一次观测**，时间戳取**桶左边界**；"
    "因此标签 t 上的值可能实际发生在 t 之后，最多晚一个桶宽（默认频率 1h 时为 1 小时）。"
    "这是有界前瞻，下游与论文必须据此解释 1 / 3 / 6 步长的结论。"
)

REPLAY_NOTE = "本次运行处理的是**历史回放 / 离线数据**，不是实时推送数据。"

DISCLAIMER_NOTE = (
    "本系统仅用于教学科研与辅助分析，不替代水行政主管部门的正式监测、预警、调度或应急决策。"
)

_MISSING = "—"


def render_quality_report(
    quality: QualitySummary,
    *,
    imputed_by_partition: Mapping[str, Mapping[str, int]],
    frequency: str,
    station_code: str | None,
    gap_note: str,
) -> str:
    """渲染质量审计 JSON：质量统计、频率、断面与每段插补占比。"""
    payload: dict[str, Any] = {
        "quality": quality.model_dump(mode="json"),
        "frequency": frequency,
        "station_code": station_code,
        "gap_note": gap_note,
        "partitions": _partition_payload(imputed_by_partition),
    }
    return _dump(payload)


def render_metrics_report(
    metrics_by_partition: Mapping[str, Mapping[int, ForecastMetrics | None]],
    *,
    horizons: tuple[int, ...],
    reasons: Mapping[str, Mapping[int, str]],
) -> str:
    """渲染预测指标 JSON：每段每步长的样本数与指标，不足时写 ``null`` 与原因。"""
    payload: dict[str, Any] = {
        "horizons": [int(horizon) for horizon in horizons],
        "notes": {
            "step_unit": STEP_UNIT_NOTE,
            "bounded_look_ahead": BOUNDED_LOOK_AHEAD_NOTE,
            "null_reasons": {
                "insufficient_samples": "该段在该步长上没有成对有效样本，指标不计算也不填 0",
                "zero_variance": "目标方差为 0，R² 与 NSE 无定义，记为 null",
            },
        },
        "partitions": {
            name: {
                "horizons": {
                    str(int(horizon)): _horizon_payload(
                        metrics_by_partition.get(name, {}).get(int(horizon)),
                        reasons.get(name, {}).get(int(horizon)),
                    )
                    for horizon in horizons
                }
            }
            for name in metrics_by_partition
        },
    }
    return _dump(payload)


def render_experiment_manifest(experiment: ExperimentManifest) -> str:
    """渲染实验清单 JSON，字段与 :class:`ExperimentManifest` 完全一致。"""
    return _dump(experiment.model_dump(mode="json"))


def render_failure_manifest(
    *,
    stage: str,
    reason: str,
    run_id: str,
    code_commit: str,
    started_at: datetime,
    finished_at: datetime,
    artifact_paths: Sequence[str],
    raw_snapshot: str | None,
) -> str:
    """渲染**切分之前**失败的 ``failure-manifest.json``（主 Agent 裁定 3）。

    此时三段区间尚未算出，而 ``ExperimentManifest`` 的
    ``train_range`` / ``validation_range`` / ``test_range`` 是非 Optional 的
    ``tuple[datetime, datetime]``，任何占位值都会变成伪造的时间区间；
    因此这里不构造实验清单，只记录失败阶段与原因。
    """
    payload: dict[str, Any] = {
        "status": "failed",
        "failure_stage": stage,
        "failure_reason": reason,
        "run_id": run_id,
        "code_commit": code_commit,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "artifact_paths": list(artifact_paths),
        "raw_snapshot": raw_snapshot,
        "note": (
            "本次运行在切分之前失败，三段时间区间尚未算出，因此不生成 "
            "experiment-manifest.json；失败阶段见 failure_stage，"
            "取值只允许 fetch / normalize / resample / split 之一。"
        ),
    }
    return _dump(payload)


def render_baseline_report(
    *,
    generated_at: datetime,
    source: SourceManifest,
    source_kind: str,
    source_label: str,
    quality: QualitySummary,
    frequency: str,
    station_code: str | None,
    station_note: str,
    split_sizes: Mapping[str, int],
    ranges: Mapping[str, tuple[datetime, datetime]],
    imputed_by_partition: Mapping[str, Mapping[str, int]],
    metrics_by_partition: Mapping[str, Mapping[int, ForecastMetrics | None]],
    reasons: Mapping[str, Mapping[int, str]],
    horizons: tuple[int, ...],
    parameters: Mapping[str, Any],
    environment: Mapping[str, str],
    code_commit: str,
    commit_note: str,
    artifacts: Mapping[str, str],
    artifact_digests: Mapping[str, str],
    gap_note: str,
    sample_note: str,
    status_note: str,
) -> str:
    """渲染人读的基线审计报告（Markdown）。"""
    lines: list[str] = []
    lines.append("# 飞云江水位基线审计报告")
    lines.append("")
    lines.append(f"- 生成时间（UTC）：{_moment(generated_at)}")
    lines.append(f"- 数据来源类型：{source_kind}")
    lines.append(f"- 数据来源：{source_label}")
    lines.append(f"- 运行状态：{status_note}")
    lines.append(
        f"- 模型：`{parameters.get('model_name', 'persistence')}`（持久性基线，无随机性，随机种子 0）"
    )
    lines.append("")
    lines.append(f"> {REPLAY_NOTE}")
    lines.append("")

    lines.append("## 1. 数据来源与获取状态")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| source_name | `{source.source_name}` |")
    lines.append(f"| source_uri | `{source.source_uri}` |")
    lines.append(f"| dataset_id | `{source.dataset_id}` |")
    lines.append(f"| 获取时间（UTC） | {_moment(source.acquired_at)} |")
    lines.append(f"| 原始记录数 | {source.record_count} |")
    lines.append(f"| 快照 SHA-256 | `{source.sha256}` |")
    lines.append("")
    lines.append(
        "- 访问审批状态：`API_AUTH_PENDING`（温州市公共数据开放平台接口凭据尚未到位）；"
        "本次未使用真实接口数据，也不得把任何凭据写入产物。"
    )
    lines.append("- 凭据只通过运行环境提供，产物、日志与异常消息都不回显凭据名称或取值。")
    lines.append("")

    lines.append("## 2. 数据时间范围与采样间隔")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 起始观测时间 | {_moment(quality.start_at)} |")
    lines.append(f"| 结束观测时间 | {_moment(quality.end_at)} |")
    lines.append(f"| 推断采样间隔（分钟） | {_number(quality.inferred_interval_minutes)} |")
    lines.append(f"| 重采样频率 | `{frequency}` |")
    lines.append("")
    lines.append(f"- {STEP_UNIT_NOTE}")
    lines.append("")

    lines.append("## 3. 断面与切分")
    lines.append("")
    lines.append(f"- 目标断面：`{station_code if station_code else 'unknown'}`")
    lines.append(f"- {station_note}")
    lines.append("")
    lines.append("| 段 | 行数 | 起始 | 结束 |")
    lines.append("| --- | --- | --- | --- |")
    for name in ("train", "validation", "test"):
        bounds = ranges.get(name)
        start = _moment(bounds[0]) if bounds else _MISSING
        end = _moment(bounds[1]) if bounds else _MISSING
        lines.append(f"| {name} | {split_sizes.get(name, 0)} | {start} | {end} |")
    lines.append("")
    lines.append("- 切分严格按时间先后，三段连续且非空，段与段之间没有间隔。")
    lines.append("")

    lines.append("## 4. 质量统计")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| total_rows | {quality.total_rows} |")
    lines.append(f"| valid_rows | {quality.valid_rows} |")
    lines.append(f"| duplicate_rows | {quality.duplicate_rows} |")
    lines.append(f"| invalid_timestamp_rows | {quality.invalid_timestamp_rows} |")
    lines.append(f"| invalid_level_rows | {quality.invalid_level_rows} |")
    lines.append(f"| missing_level_rows | {quality.missing_level_rows} |")
    lines.append("")
    lines.append(
        "计数满足守恒式 `total_rows == valid_rows + invalid_timestamp_rows + "
        "invalid_level_rows + missing_level_rows + duplicate_rows`；"
        "`z_id` 不可用的行计入 `invalid_timestamp_rows`（上游已记录该偏差）。"
    )
    lines.append("")

    lines.append("## 5. 插补与缺口")
    lines.append("")
    lines.append(f"- {gap_note}")
    lines.append("")
    lines.append("| 段 | 行数 | 插补行 | 插补占比 | 参与评估行 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for name, counts in imputed_by_partition.items():
        rows = counts.get("rows", 0)
        imputed = counts.get("imputed", 0)
        ratio = 0.0 if rows == 0 else imputed / rows
        evaluated: object = counts.get("evaluated", 0)
        if name == "train":
            evaluated = "不适用（训练集不参与评估）"
        lines.append(f"| {name} | {rows} | {imputed} | {ratio:.3f} | {evaluated} |")
    lines.append("")
    lines.append(
        "- 评估前已剔除 `is_imputed=True` 与水位缺失的行（约束 C-5）："
        "插补值是模型产物，不能当作标签使用。"
    )
    lines.append("")

    lines.append("## 6. 基线预测指标")
    lines.append("")
    lines.append("| 段 | 步长 | 样本数 | MAE | RMSE | R² | NSE | 峰值绝对误差 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name in metrics_by_partition:
        for horizon in horizons:
            metrics = metrics_by_partition.get(name, {}).get(int(horizon))
            reason = reasons.get(name, {}).get(int(horizon))
            if metrics is None:
                lines.append(
                    f"| {name} | {int(horizon)} | 0 | "
                    f"{_MISSING} | {_MISSING} | {_MISSING} | {_MISSING} | {_MISSING} | "
                    f"样本不足（{reason or 'insufficient_samples'}） |"
                )
                continue
            lines.append(
                f"| {name} | {int(horizon)} | {metrics.sample_count} | "
                f"{_number(metrics.mae)} | {_number(metrics.rmse)} | "
                f"{_number(metrics.r2)} | {_number(metrics.nse)} | "
                f"{_number(metrics.peak_absolute_error)} | 正常计算 |"
            )
    lines.append("")
    lines.append("- 基线为持久性模型：步长 h 的预测取该行之前第 h 步的观测值。")
    lines.append(
        "- 样本不足时该段该步长的指标为 `null` 且给出 `reason`，绝不填 0、绝不跳过该步长。"
    )
    lines.append("- R² / NSE 为 `null` 有两种情形：目标方差为 0，或样本不足未计算。")
    lines.append("")

    lines.append("## 7. 有界前瞻声明（必须随结论一起引用）")
    lines.append("")
    lines.append(f"- {BOUNDED_LOOK_AHEAD_NOTE}")
    lines.append(f"- {STEP_UNIT_NOTE}")
    lines.append("")

    lines.append("## 8. 运行参数、环境与代码版本")
    lines.append("")
    lines.append("| 参数 | 值 |")
    lines.append("| --- | --- |")
    for key, value in parameters.items():
        lines.append(f"| {key} | `{_scalar(value)}` |")
    lines.append("")
    lines.append("| 环境项 | 值 |")
    lines.append("| --- | --- |")
    for key, value in environment.items():
        lines.append(f"| {key} | `{value}` |")
    lines.append("")
    lines.append(f"- 代码提交号：`{code_commit}`")
    if commit_note:
        lines.append(f"- 提交号说明：{commit_note}")
    lines.append("")

    lines.append("## 9. 产物清单与校验值")
    lines.append("")
    lines.append("| 产物 | 相对路径 | SHA-256 |")
    lines.append("| --- | --- | --- |")
    for name, relative in artifacts.items():
        lines.append(f"| {name} | `{relative}` | `{artifact_digests.get(name, _MISSING)}` |")
    lines.append("")

    lines.append("## 10. 局限与未决问题")
    lines.append("")
    lines.append(f"- {sample_note}")
    lines.append("- 本次是持久性基线，不是机器学习模型结论，仅作为后续模型的对照下限。")
    lines.append(
        "- 切分之间不留间隔，段首若干步长的预测来自上一段末尾的观测（跨段窗口禁止用于训练）。"
    )
    lines.append("- 样例与离线快照都不能代表真实洪峰过程，超警事件指标尚未评估。")
    lines.append("- 接口凭据仍在审批中（`API_AUTH_PENDING`），真实数据结论待凭据到位后重跑。")
    lines.append("")
    lines.append("## 11. 使用声明")
    lines.append("")
    lines.append(f"- {DISCLAIMER_NOTE}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 渲染辅助
# --------------------------------------------------------------------------- #


def _dump(payload: Mapping[str, Any]) -> str:
    """序列化为严格 JSON：禁止 ``NaN`` / ``Infinity`` 字面量。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)


def _partition_payload(imputed_by_partition: Mapping[str, Mapping[str, int]]) -> dict[str, Any]:
    """把每段的行数、插补行数与占比写成稳定结构。"""
    payload: dict[str, Any] = {}
    for name, counts in imputed_by_partition.items():
        rows = int(counts.get("rows", 0))
        imputed = int(counts.get("imputed", 0))
        payload[name] = {
            "rows": rows,
            "imputed": imputed,
            "missing": int(counts.get("missing", 0)),
            "evaluated": int(counts.get("evaluated", 0)),
            "imputed_ratio": 0.0 if rows == 0 else round(imputed / rows, 6),
        }
    return payload


def _horizon_payload(metrics: ForecastMetrics | None, reason: str | None) -> dict[str, Any]:
    """把一段一个步长的结果写成 JSON；未计算时保留样本数 0 与原因。"""
    if metrics is None:
        return {
            "sample_count": 0,
            "mae": None,
            "rmse": None,
            "r2": None,
            "nse": None,
            "peak_absolute_error": None,
            "reason": reason or "insufficient_samples",
        }
    return {
        "sample_count": metrics.sample_count,
        "mae": metrics.mae,
        "rmse": metrics.rmse,
        "r2": metrics.r2,
        "nse": metrics.nse,
        "peak_absolute_error": metrics.peak_absolute_error,
        "reason": reason,
    }


def _moment(value: datetime | None) -> str:
    return _MISSING if value is None else value.isoformat()


def _number(value: float | None) -> str:
    return _MISSING if value is None else f"{value:.6f}"


def _scalar(value: Any) -> str:
    if isinstance(value, tuple | list):
        return ", ".join(str(item) for item in value)
    return str(value)

"""Structured water brief built only from structured results (DS-8A).

简报不接受自由文本数值：所有数字都来自历史、预测、风险与对比服务的结果对象，
缺失即写"不可用"，不补造。
"""

from __future__ import annotations

from datetime import datetime

from ..config import Settings
from ..errors import DataUnavailableError
from ..schemas.common import (
    DISCLAIMER,
    RESEARCH_THRESHOLD,
    ResponseStatus,
)
from ..schemas.domain import (
    BriefResult,
    BriefSection,
    EventMetricCell,
    EvidenceBundle,
    ForecastResult,
    ModelComparison,
    RiskResult,
    WaterLevelPoint,
)

DATA_MODE_LABEL = {"history_replay": "历史回放", "local_snapshot": "本地快照"}


class BriefService:
    """Composes the structured water brief from an evidence bundle."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build(self, bundle: EvidenceBundle) -> BriefResult:
        """Build a brief from structured results only.

        Raises:
            DataUnavailableError: 没有任何可用证据（既无观测也无预测）。
        """
        if bundle.latest is None and bundle.forecast is None:
            raise DataUnavailableError("没有可用证据（缺少观测与预测结果），无法生成简报")

        generated_at = self._generated_at(bundle)
        data_label = DATA_MODE_LABEL.get(bundle.data_mode, bundle.data_mode)
        warnings: list[str] = list(bundle.warnings)
        statuses: list[ResponseStatus] = []
        sections: list[BriefSection] = []
        highlights: list[str] = []

        sections.append(self._data_section(bundle, data_label))
        if bundle.latest is not None:
            highlights.append(
                f"最新观测 {bundle.latest.water_level_m:.2f} m"
                f"（{bundle.latest.observed_at.isoformat()}，{data_label}）"
            )
        if bundle.forecast is not None:
            statuses.append(bundle.forecast.status)
            warnings.extend(bundle.forecast.warnings)
            sections.append(self._forecast_section(bundle.forecast))
            highlights.append(
                f"预测模型 {bundle.forecast.model}/{bundle.forecast.model_version}"
                f"（状态 {bundle.forecast.status}）"
            )
        if bundle.risk is not None:
            statuses.append(bundle.risk.status)
            warnings.extend(bundle.risk.warnings)
            sections.append(self._risk_section(bundle.risk))
            highlights.append(f"研究性判定等级：{bundle.risk.level}")
        if bundle.comparison is not None:
            statuses.append(bundle.comparison.status)
            warnings.extend(bundle.comparison.warnings)
            sections.append(self._comparison_section(bundle.comparison))

        evidence = []
        for result in (bundle.forecast, bundle.risk, bundle.comparison):
            if result is not None:
                evidence.extend(getattr(result, "evidence", []))

        return BriefResult(
            title=f"{bundle.station_id} 水情简报（{data_label}）",
            station_id=bundle.station_id,
            data_mode=bundle.data_mode,
            generated_at=generated_at,
            data_time=bundle.latest.observed_at if bundle.latest else None,
            model_version=bundle.forecast.model_version if bundle.forecast else None,
            risk_level=bundle.risk.level if bundle.risk else None,
            highlights=highlights,
            sections=sections,
            tool_trace=list(bundle.tool_trace),
            status=self._worst(statuses),
            warnings=warnings,
            evidence=evidence,
        )

    @staticmethod
    def _generated_at(bundle: EvidenceBundle) -> datetime:
        """Deterministic generation time: latest data time, never the wall clock."""
        if bundle.forecast is not None:
            return bundle.forecast.as_of
        if bundle.latest is not None:
            return bundle.latest.observed_at
        raise DataUnavailableError("没有可用证据（缺少观测与预测结果），无法生成简报")

    @staticmethod
    def _worst(statuses: list[ResponseStatus]) -> ResponseStatus:
        if "unavailable" in statuses:
            return "unavailable"
        if "degraded" in statuses:
            return "degraded"
        return "ok"

    def _data_section(self, bundle: EvidenceBundle, data_label: str) -> BriefSection:
        lines = [
            f"数据模式：{data_label}（{bundle.data_mode}）",
            f"目标断面：{bundle.station_id}",
            f"数据来源：{self._settings.source_url}",
        ]
        latest: WaterLevelPoint | None = bundle.latest
        if latest is None and bundle.forecast is not None:
            lines.append("最新观测：无（预测结果未提供观测点）")
        if latest is not None:
            lines.append(
                f"最新观测：{latest.water_level_m:.3f} m（{latest.observed_at.isoformat()}）"
            )
        lines.append(DISCLAIMER)
        return BriefSection(heading="数据与水情", lines=lines)

    @staticmethod
    def _forecast_section(forecast: ForecastResult) -> BriefSection:
        lines = [
            f"模型：{forecast.model} / {forecast.model_version}"
            f"（请求模型 {forecast.requested_model}）",
            f"锚点：{forecast.as_of.isoformat()}；状态：{forecast.status}",
        ]
        if forecast.fallback_reason:
            lines.append(f"回退原因：{forecast.fallback_reason}")
        for point in forecast.predictions:
            value = (
                f"{point.water_level_m:.3f} m"
                if point.water_level_m is not None
                else "不可用（不补造）"
            )
            lines.append(
                f"未来 h{point.horizon}（{point.target_at.isoformat()}）：{value}[{point.source}]"
            )
        if forecast.is_stale:
            lines.append(
                f"数据陈旧：距最后观测 {forecast.data_age_minutes:.0f} 分钟，已超过陈旧门槛"
            )
        return BriefSection(heading="预测（未来 1/3/6 步）", lines=lines)

    @staticmethod
    def _risk_section(risk: RiskResult) -> BriefSection:
        triggered = "、".join(f"h{step}" for step in risk.triggered_horizons) or "无"
        lines = [
            f"研究性判定等级：{risk.level}（{risk.metric_name}）",
            f"研究性阈值：{risk.threshold:.2f} {risk.threshold_unit}（{risk.threshold_source}）",
            f"触发时域：{triggered}；缺失不参评："
            f"{'、'.join(f'h{step}' for step in risk.missing_horizons) or '无'}",
            f"等级来源：{risk.basis_source}（确定性规则，Provider 不得修改）",
            f"数据：最后观测 {risk.last_observed_at.isoformat()}，"
            f"数据龄 {risk.data_age_hours:.2f} 小时，陈旧标记 {risk.is_stale}",
        ]
        for limitation in risk.limitations[:2]:
            lines.append(f"限制：{limitation}")
        return BriefSection(heading="研究性风险判定", lines=lines)

    @staticmethod
    def _comparison_section(comparison: ModelComparison) -> BriefSection:
        lines = [
            f"指标来源：{comparison.source}；状态：{comparison.status}",
            f"研究性阈值：{comparison.threshold:.2f} {comparison.threshold_unit}",
            f"主对照：{', '.join(comparison.primary_comparison)}；{comparison.primary_note}",
        ]
        for cell in comparison.metrics:
            if cell.model not in comparison.primary_comparison:
                continue
            lines.append(
                f"test h{cell.horizon} {cell.display_name}：样本 {cell.sample_count}，"
                f"MAE {cell.mae:.6f}，RMSE {cell.rmse:.6f}，R² {cell.r2:.6f}"
            )
        test_events = comparison.event_metrics.get("test", {})
        for horizon_key, models in sorted(test_events.items()):
            cell: EventMetricCell | None = models.get("xgboost_e1") or next(
                iter(models.values()), None
            )
            if cell is None:
                continue
            metric = (
                "null（reason=" + str(cell.reason) + "）"
                if cell.precision is None
                else f"{cell.precision:.6f}"
            )
            lines.append(f"test {horizon_key} 事件级 F1/precision：{metric}")
        lines.append(f"本简报只消费已发布指标，不重算；阈值 {RESEARCH_THRESHOLD} m 非官方标准。")
        return BriefSection(heading="模型对比与限制", lines=lines)

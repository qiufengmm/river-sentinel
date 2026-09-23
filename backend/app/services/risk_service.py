"""Deterministic research risk evaluation (DS-8A).

风险等级**只能**由 ``river_sentinel_ml.risk.evaluate_research_risk`` 产生：本服务不复制
阈值判断、不调整等级、不接受 Provider 或调用方给出的等级。研究性高水位阈值沿用 DS-7
train p90 = 7.18 m，**不是官方警戒、超警或正式预警标准**。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import pandas as pd
from river_sentinel_ml.risk import evaluate_research_risk, render_verdict

from ..config import Settings
from ..errors import InvalidRequestError, ModelUnavailableError
from ..schemas.common import (
    MISSING_HORIZONS,
    RESEARCH_THRESHOLD,
    RESEARCH_THRESHOLD_SOURCE,
    STALE_SNAPSHOT,
    Evidence,
    ResponseStatus,
    warning_of,
)
from ..schemas.domain import ForecastResult, RiskRequest, RiskResult
from ..timeutils import ensure_aware

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from .forecast_service import ForecastService

#: 单一水位输入按持久性假设代入各时域时的说明。
SINGLE_LEVEL_NOTE = "SINGLE_LEVEL_PERSISTENCE_ASSUMPTION"

RISK_LIMITATIONS: tuple[str, ...] = (
    "研究性高水位阈值只由 train 段 p90 拟合，非官方标准，仅用于教学科研辅助，不替代官方防汛决策。",
    "判定只依据给定预测值与阈值比较，不含降雨、流量、调度与河道地形等真实水文条件。",
    "数据陈旧只做标注：等级不因陈旧而自动上调或下调。",
    "test 段在 train p90 阈值下零观测事件，事件级检出能力尚未得到验证。",
)


class RiskService:
    """Wraps the deterministic research risk rule with evidence and caveats."""

    def __init__(self, settings: Settings, forecast: "ForecastService | None" = None) -> None:
        self._settings = settings
        self._forecast = forecast

    def evaluate(self, request: RiskRequest) -> RiskResult:
        """Evaluate the research risk verdict for one request.

        ``predictions`` 缺省时按持久性假设把 ``water_level`` 代入每个请求时域，并在
        ``warnings`` 中显式标注该假设。
        """
        if request.station_id != self._settings.station_id:
            raise InvalidRequestError(
                f"未知断面 {request.station_id!r}；本服务只服务 {self._settings.station_id!r}"
            )
        observed_at = ensure_aware(request.observed_at, self._settings.timezone)
        anchor_at = request.as_of or observed_at
        anchor_at = ensure_aware(anchor_at, self._settings.timezone)
        if anchor_at < observed_at:
            raise InvalidRequestError(
                f"as_of={anchor_at.isoformat()} 早于观测时间 {observed_at.isoformat()}"
            )

        warnings: list[str] = []
        if request.predictions is None:
            values = {horizon: float(request.water_level) for horizon in request.horizons}
            model = "persistence"
            model_version = "single_level_input"
            warnings.append(
                warning_of(
                    SINGLE_LEVEL_NOTE,
                    "未提供逐时域预测序列：按持久性假设把给定水位 "
                    f"{request.water_level:.3f} m 代入各请求时域，仅用于教学演示",
                )
            )
        else:
            values = {
                horizon: float(request.predictions[horizon])
                for horizon in request.horizons
                if horizon in request.predictions
            }
            model = request.model
            model_version = request.model_version

        return self._verdict(
            station_id=request.station_id,
            values=values,
            horizons=request.horizons,
            anchor_at=anchor_at,
            observed_at=observed_at,
            model=model,
            model_version=model_version,
            data_mode=request.data_mode,
            warnings=warnings,
            tool_trace=[
                "evaluate_research_risk:"
                f"threshold={RESEARCH_THRESHOLD}:horizons="
                f"{','.join(str(horizon) for horizon in request.horizons)}"
            ],
        )

    def evaluate_forecast(
        self,
        result: ForecastResult,
        *,
        data_mode: str | None = None,
        tool_trace: list[str] | None = None,
    ) -> RiskResult:
        """Evaluate risk from an existing forecast result (agent tool path)."""
        values = {
            point.horizon: point.water_level_m
            for point in result.predictions
            if point.water_level_m is not None
        }
        if not values:
            raise ModelUnavailableError("预测值全部不可用，无法进行风险判定（不补造数值）")
        if result.anchor_at is None:
            raise ModelUnavailableError("预测缺少锚点时间，无法进行风险判定")

        return self._verdict(
            station_id=result.station_id,
            values=values,
            horizons=[point.horizon for point in result.predictions],
            anchor_at=result.as_of,
            observed_at=result.anchor_at,
            model=result.model,
            model_version=result.model_version,
            data_mode=data_mode or self._settings.data_mode,
            warnings=list(result.warnings),
            tool_trace=tool_trace
            or [
                "evaluate_research_risk:"
                f"model={result.model}:horizons="
                f"{','.join(str(point.horizon) for point in result.predictions)}"
            ],
        )

    def _verdict(
        self,
        *,
        station_id: str,
        values: dict[int, float],
        horizons: list[int],
        anchor_at: datetime,
        observed_at: datetime,
        model: str,
        model_version: str,
        data_mode: str,
        warnings: list[str],
        tool_trace: list[str],
    ) -> RiskResult:
        verdict = evaluate_research_risk(
            predictions=values,
            threshold=RESEARCH_THRESHOLD,
            anchor_at=pd.Timestamp(anchor_at),
            last_observed_at=pd.Timestamp(observed_at),
            threshold_source=RESEARCH_THRESHOLD_SOURCE,
            model_name=model,
            model_version=model_version,
            horizons=tuple(horizons),
            stale_after_hours=self._settings.stale_after_minutes / 60.0,
        )
        basis = dict(verdict.basis)
        warnings = list(warnings)
        if verdict.is_stale:
            warnings.append(
                warning_of(
                    STALE_SNAPSHOT,
                    f"判定的数据基础已落后 {basis.get('data_age_hours')} 小时"
                    f"（陈旧门槛 {basis.get('stale_after_hours')} 小时），等级未自动调整",
                )
            )
        missing = [int(step) for step in basis.get("missing_horizons", ())]
        if missing:
            warnings.append(
                warning_of(
                    MISSING_HORIZONS,
                    f"以下时域缺少可用预测，不参与判定：{', '.join(f'h{step}' for step in missing)}",
                )
            )
        status: ResponseStatus = "degraded" if (verdict.is_stale or missing) else "ok"
        predicted_values = {
            str(key): (None if value is None else float(value))
            for key, value in dict(basis.get("predicted_values", {})).items()
        }
        evidence = [
            Evidence(
                source_url=self._settings.source_url,
                data_mode=data_mode,
                observed_at=observed_at,
                freshness="stale" if verdict.is_stale else "fresh",
                model_version=model_version,
                tool_trace=tool_trace,
                notes=[
                    f"研究性阈值：{RESEARCH_THRESHOLD} m（{RESEARCH_THRESHOLD_SOURCE}）",
                    "等级来源：river_sentinel_ml.risk.evaluate_research_risk",
                ],
            )
        ]
        return RiskResult(
            station_id=station_id,
            level=verdict.level,
            is_stale=verdict.is_stale,
            threshold=float(basis["threshold"]),
            threshold_source=str(basis["threshold_source"]),
            triggered_horizons=[int(step) for step in verdict.triggered_horizons],
            missing_horizons=missing,
            predicted_values=predicted_values,
            anchor_at=datetime.fromisoformat(str(basis["anchor_at"])),
            last_observed_at=datetime.fromisoformat(str(basis["data_last_observed_at"])),
            evaluated_at=datetime.fromisoformat(str(basis["evaluated_at"])),
            data_age_hours=float(basis["data_age_hours"]),
            model=model,
            model_version=model_version,
            basis=basis,
            guidance=render_verdict(verdict),
            limitations=list(RISK_LIMITATIONS),
            status=status,
            warnings=warnings,
            evidence=evidence,
        )

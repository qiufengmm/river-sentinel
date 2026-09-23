"""Forecast service with deterministic persistence fallback (DS-8A).

规则：

1. 先按显式配置的产物目录查找模型预测；
2. 找不到（首版必然找不到：仓库不含模型权重）时**只能**回退已验证的持久性基线，并
   标记 ``status=degraded`` + ``fallback_reason`` + 基线版本；
3. 连基线都不够历史步数时返回 ``status=unavailable``，预测值写 ``None``；
4. 禁止用 ``0``、随机数或大语言模型文本替代预测值。
"""

from __future__ import annotations

import pandas as pd

from river_sentinel_ml.baseline import persistence_forecast

from ..config import Settings
from ..errors import DataUnavailableError, InvalidRequestError
from ..repositories.artifact_repository import ArtifactRepository
from ..repositories.sample_repository import SampleRepository
from ..schemas.common import (
    MODEL_UNAVAILABLE,
    PERSISTENCE_MODEL,
    PERSISTENCE_VERSION,
    STALE_SNAPSHOT,
    Evidence,
    ResponseStatus,
    warning_of,
)
from ..schemas.domain import ForecastRequest, ForecastResult, PredictionPoint
from ..timeutils import age_minutes, ensure_aware, freshness_of, infer_step_minutes, shift_steps


class ForecastService:
    """Produces multi-horizon forecasts or an explicit unavailable state."""

    def __init__(
        self,
        samples: SampleRepository,
        artifacts: ArtifactRepository,
        settings: Settings,
    ) -> None:
        self._samples = samples
        self._artifacts = artifacts
        self._settings = settings

    def forecast(self, request: ForecastRequest) -> ForecastResult:
        """Forecast ``request.horizons`` steps ahead from ``as_of``."""
        station = request.station_id
        if station != self._settings.station_id:
            raise InvalidRequestError(
                f"未知断面 {station!r}；本服务只服务 {self._settings.station_id!r}"
            )

        series = self._samples.level_series(None)
        if series.empty:
            raise DataUnavailableError("样例数据为空，无法生成预测")

        last_observed = ensure_aware(series.index[-1].to_pydatetime(), self._settings.timezone)
        as_of = request.as_of or last_observed
        as_of = ensure_aware(as_of, self._settings.timezone)

        history = series.loc[series.index <= as_of]
        if history.empty:
            raise DataUnavailableError(f"as_of={as_of.isoformat()} 之前没有可用观测，无法生成预测")

        anchor = ensure_aware(history.index[-1].to_pydatetime(), self._settings.timezone)
        step_minutes = infer_step_minutes(series.index)
        age = age_minutes(anchor, as_of)
        freshness = freshness_of(age, self._settings.stale_after_minutes)
        is_stale = freshness == "stale"

        horizons = sorted(set(request.horizons))
        warnings: list[str] = []
        if is_stale:
            warnings.append(
                warning_of(
                    STALE_SNAPSHOT,
                    f"锚点 {as_of.isoformat()} 距最后观测 {anchor.isoformat()} 已 {age:.0f} 分钟，"
                    f"超过陈旧门槛 {self._settings.stale_after_minutes} 分钟",
                )
            )

        artifact_points, artifact_version = self._from_artifact(request.model, horizons, as_of)
        if artifact_points is not None:
            model = request.model
            model_version = artifact_version or "unknown"
            status: ResponseStatus = "degraded" if is_stale else "ok"
            fallback_reason = None
            points = artifact_points
        else:
            points = self._from_persistence(history, horizons, as_of, step_minutes)
            model = PERSISTENCE_MODEL
            model_version = PERSISTENCE_VERSION
            if request.model != PERSISTENCE_MODEL:
                status = "degraded"
                fallback_reason = "model_artifact_unavailable"
                warnings.append(
                    warning_of(
                        MODEL_UNAVAILABLE,
                        f"未找到 {request.model} 的模型产物，已回退已验证基线 "
                        f"{PERSISTENCE_MODEL}/{PERSISTENCE_VERSION}；结果不得标注为 {request.model}",
                    )
                )
            else:
                status = "degraded" if is_stale else "ok"
                fallback_reason = None
            if all(point.water_level_m is None for point in points):
                status = "unavailable"
                fallback_reason = "insufficient_history"
                warnings.append(
                    warning_of(
                        MODEL_UNAVAILABLE,
                        "历史步数不足，持久性基线无法给出任何时域预测；不补造数值",
                    )
                )

        evidence = [
            Evidence(
                source_url=self._settings.source_url,
                data_mode=request.data_mode,
                observed_at=anchor,
                freshness=freshness,
                model_version=model_version,
                tool_trace=[
                    f"forecast_water_level:requested={request.model}:effective={model}"
                    f":horizons={','.join(str(h) for h in horizons)}"
                ],
                notes=[
                    f"预测锚点：{as_of.isoformat()}",
                    f"步长口径：{step_minutes:.0f} 分钟/步，1/3/6 步按步长外推",
                ],
            )
        ]
        return ForecastResult(
            station_id=station,
            requested_model=request.model,
            model=model,
            model_version=model_version,
            as_of=as_of,
            anchor_at=anchor,
            step_minutes=step_minutes,
            horizons=horizons,
            predictions=points,
            status=status,
            fallback_reason=fallback_reason,
            freshness=freshness,
            is_stale=is_stale,
            data_age_minutes=age,
            warnings=warnings,
            evidence=evidence,
        )

    def _from_artifact(
        self,
        model: str,
        horizons: list[int],
        as_of,
    ) -> tuple[list[PredictionPoint] | None, str | None]:
        """Look up prediction artifacts for every horizon; ``None`` when incomplete."""
        if model == PERSISTENCE_MODEL:
            return None, None
        points: list[PredictionPoint] = []
        version: str | None = None
        for horizon in horizons:
            series = self._artifacts.find_prediction(model, horizon, as_of)
            if series is None or not series.points:
                return None, None
            points.append(series.points[0])
            version = version or series.model_version
        return points, version

    def _from_persistence(
        self,
        history: pd.Series,
        horizons: list[int],
        as_of,
        step_minutes: float,
    ) -> list[PredictionPoint]:
        """Persistence baseline: the observation ``h`` steps before the anchor row."""
        frame = persistence_forecast(history, tuple(horizons))
        row = frame.iloc[-1]
        points: list[PredictionPoint] = []
        for horizon in horizons:
            raw = row[f"prediction_h{horizon}"]
            value = None if pd.isna(raw) else float(raw)
            points.append(
                PredictionPoint(
                    horizon=horizon,
                    target_at=shift_steps(as_of, horizon, step_minutes),
                    water_level_m=value,
                    source="persistence",
                )
            )
        return points

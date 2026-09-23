"""Model comparison service: consumes DS-7 metrics, never retrains (DS-8A)."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..errors import InvalidRequestError
from ..repositories.artifact_repository import ArtifactRepository
from ..schemas.common import (
    COMPARISON_ARTIFACT_UNAVAILABLE,
    SUPPORTED_HORIZONS,
    Evidence,
    ResponseStatus,
    warning_of,
)
from ..schemas.domain import EventMetricCell, MetricCell, ModelComparison
from . import comparison_snapshot as snapshot


class ComparisonService:
    """Exposes the DS-7 model comparison with sample counts and limitations."""

    def __init__(self, artifacts: ArtifactRepository, settings: Settings) -> None:
        self._artifacts = artifacts
        self._settings = settings

    def compare(self, horizons: list[int] | None = None) -> ModelComparison:
        """Return metrics, event metrics and limitations for the requested horizons."""
        wanted = self._validate_horizons(horizons)
        artifact = self._artifacts.load_comparison()
        warnings: list[str] = []
        if artifact is None:
            status: ResponseStatus = "degraded"
            source = f"{snapshot.SOURCE_REPORT}（{snapshot.SOURCE_SECTIONS}）"
            version = snapshot.SNAPSHOT_VERSION
            warnings.append(
                warning_of(
                    COMPARISON_ARTIFACT_UNAVAILABLE,
                    "未配置产物目录或缺少 model_comparison.json，"
                    "返回 DS-7 报告已发布数值快照（只读，未重算）",
                )
            )
            metrics = self._metrics_from_snapshot(wanted)
            event_metrics = self._events_from_snapshot(wanted)
            threshold = snapshot.THRESHOLD
            threshold_source = snapshot.THRESHOLD_SOURCE
            limitations = list(snapshot.LIMITATIONS)
            primary = list(snapshot.PRIMARY_COMPARISON)
            primary_note = snapshot.PRIMARY_NOTE
        else:
            status = "ok"
            source = str(artifact.get("source", self._settings.comparison_artifact_path))
            version = str(artifact.get("version", "configured_artifact"))
            threshold = float(artifact.get("threshold", snapshot.THRESHOLD))
            threshold_source = str(artifact.get("threshold_source", snapshot.THRESHOLD_SOURCE))
            limitations = [str(item) for item in artifact.get("limitations", [])]
            primary = [str(item) for item in artifact.get("primary_comparison", [])]
            primary_note = str(artifact.get("primary_note", snapshot.PRIMARY_NOTE))
            metrics = self._metrics_from_artifact(artifact, wanted)
            event_metrics = self._events_from_artifact(artifact, wanted)

        evidence = [
            Evidence(
                source_url=self._settings.source_url,
                data_mode=self._settings.data_mode,
                observed_at=None,
                freshness="unknown",
                model_version=version,
                tool_trace=[
                    "compare_forecast_models:"
                    f"horizons={','.join(str(horizon) for horizon in wanted)}"
                ],
                notes=[f"指标来源：{source}"],
            )
        ]
        return ModelComparison(
            threshold=threshold,
            threshold_source=threshold_source,
            primary_comparison=primary,
            primary_note=primary_note,
            metrics=metrics,
            event_metrics=event_metrics,
            limitations=limitations,
            source=source,
            status=status,
            warnings=warnings,
            evidence=evidence,
        )

    @staticmethod
    def _validate_horizons(horizons: list[int] | None) -> list[int]:
        if horizons is None:
            return list(SUPPORTED_HORIZONS)
        if not horizons:
            raise InvalidRequestError("horizons must not be empty")
        result: list[int] = []
        for horizon in horizons:
            if isinstance(horizon, bool) or horizon not in SUPPORTED_HORIZONS:
                raise InvalidRequestError(
                    f"horizon must be one of {SUPPORTED_HORIZONS}, got {horizon!r}"
                )
            if horizon not in result:
                result.append(horizon)
        return sorted(result)

    @staticmethod
    def _metrics_from_snapshot(horizons: list[int]) -> list[MetricCell]:
        return [
            MetricCell(
                segment=snapshot.SEGMENT,
                horizon=horizon,
                model=model,
                display_name=snapshot.MODEL_DISPLAY.get(model, model),
                model_version=snapshot.MODEL_VERSION.get(model, "unknown"),
                sample_count=count,
                mae=mae,
                rmse=rmse,
                r2=r2,
                nse=nse,
            )
            for model, horizon, count, mae, rmse, r2, nse in snapshot.METRICS
            if horizon in horizons
        ]

    @staticmethod
    def _events_from_snapshot(
        horizons: list[int],
    ) -> dict[str, dict[str, dict[str, EventMetricCell]]]:
        grid: dict[str, dict[str, dict[str, EventMetricCell]]] = {}
        for row in snapshot.EVENT_METRICS:
            model, horizon, observed_points, observed_segments, predicted_points = row[:5]
            precision, recall, f1, reason = row[5], row[6], row[7], row[8]
            if horizon not in horizons:
                continue
            # 报告只列出观测 / 预测事件点数：观测事件点为 0 时 tp=fn=0、误报数等于预测
            # 事件点；其余格子不猜测，保持 0 并让 precision/recall/f1 沿用报告值。
            false_positive = predicted_points if observed_points == 0 else 0
            cell = EventMetricCell(
                segment=snapshot.SEGMENT,
                horizon=horizon,
                model=model,
                display_name=snapshot.MODEL_DISPLAY.get(model, model),
                precision=precision,
                recall=recall,
                f1=f1,
                tp=0,
                fp=false_positive,
                fn=0,
                observed_event_points=observed_points,
                observed_event_segments=observed_segments,
                predicted_event_points=predicted_points,
                predicted_event_segments=None,
                reason=reason,
            )
            grid.setdefault(snapshot.SEGMENT, {}).setdefault(f"h{horizon}", {})[model] = cell
        return grid

    @staticmethod
    def _metrics_from_artifact(artifact: dict[str, Any], horizons: list[int]) -> list[MetricCell]:
        cells: list[MetricCell] = []
        for row in artifact.get("metrics", []):
            if not isinstance(row, dict) or int(row.get("horizon", -1)) not in horizons:
                continue
            model = str(row.get("model", "unknown"))
            cells.append(
                MetricCell(
                    segment=str(row.get("segment", "test")),
                    horizon=int(row["horizon"]),
                    model=model,
                    display_name=str(row.get("display_name", model)),
                    model_version=str(row.get("model_version", "unknown")),
                    sample_count=None
                    if row.get("sample_count") is None
                    else int(row["sample_count"]),
                    mae=None if row.get("mae") is None else float(row["mae"]),
                    rmse=None if row.get("rmse") is None else float(row["rmse"]),
                    r2=None if row.get("r2") is None else float(row["r2"]),
                    nse=None if row.get("nse") is None else float(row["nse"]),
                    reason=None if row.get("reason") is None else str(row["reason"]),
                )
            )
        return cells

    @classmethod
    def _events_from_artifact(
        cls,
        artifact: dict[str, Any],
        horizons: list[int],
    ) -> dict[str, dict[str, dict[str, EventMetricCell]]]:
        grid: dict[str, dict[str, dict[str, EventMetricCell]]] = {}
        raw = artifact.get("event_metrics")
        if not isinstance(raw, dict):
            return grid
        for segment, horizons_map in raw.items():
            if not isinstance(horizons_map, dict):
                continue
            for horizon_key, models in horizons_map.items():
                if not isinstance(models, dict):
                    continue
                try:
                    horizon = int(str(horizon_key).lstrip("h"))
                except ValueError:
                    continue
                if horizon not in horizons:
                    continue
                for model, row in models.items():
                    if not isinstance(row, dict):
                        continue
                    grid.setdefault(str(segment), {}).setdefault(f"h{horizon}", {})[str(model)] = (
                        EventMetricCell(
                            segment=str(segment),
                            horizon=horizon,
                            model=str(model),
                            display_name=str(row.get("display_name", model)),
                            precision=_opt_float(row.get("precision")),
                            recall=_opt_float(row.get("recall")),
                            f1=_opt_float(row.get("f1")),
                            tp=int(row.get("tp", 0) or 0),
                            fp=int(row.get("fp", 0) or 0),
                            fn=int(row.get("fn", 0) or 0),
                            observed_event_points=int(row.get("observed_event_points", 0) or 0),
                            observed_event_segments=int(row.get("observed_event_segments", 0) or 0),
                            predicted_event_points=int(row.get("predicted_event_points", 0) or 0),
                            predicted_event_segments=(
                                None
                                if row.get("predicted_event_segments") is None
                                else int(row["predicted_event_segments"])
                            ),
                            reason=None if row.get("reason") is None else str(row["reason"]),
                        )
                    )
        return grid


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)

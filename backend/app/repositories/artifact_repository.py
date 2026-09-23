"""Read-only repository over explicitly configured local artifacts (DS-8A).

产物目录只能由 ``Settings.artifact_dir``（或 ``RIVER_SENTINEL_ARTIFACT_DIR``）传入；
未配置或文件缺失时一律返回 ``None``，由调用方走已验证的持久性基线回退，
**不补造预测值**。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from ..config import Settings
from ..schemas.domain import PredictionPoint, PredictionSeries


class ArtifactRepository:
    """Looks up prediction artifacts and optional comparison artifacts on disk."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def artifact_dir(self) -> Path | None:
        """Configured artifact directory (``None`` when unset)."""
        return self._settings.resolved_artifact_dir

    def _candidates(self, model: str, horizon: int) -> list[Path]:
        directory = self.artifact_dir
        if directory is None:
            return []
        names = (
            f"{model}_h{horizon}.json",
            f"{model}.json",
            f"prediction_{model}_h{horizon}.json",
        )
        candidates: list[Path] = []
        for name in names:
            candidates.append(directory / name)
            candidates.append(directory / "predictions" / name)
        return candidates

    def find_prediction(
        self,
        model: str,
        horizon: int,
        as_of: datetime,
    ) -> PredictionSeries | None:
        """Return the newest artifact for ``model`` / ``horizon`` not later than ``as_of``.

        约定格式（JSON）：

        ```json
        {
          "model": "xgboost",
          "model_version": "...",
          "as_of": "2026-01-01T11:00:00+08:00",
          "anchor_at": "2026-01-01T11:00:00+08:00",
          "points": [{"horizon": 1, "target_at": "...", "water_level_m": 3.71}]
        }
        ```

        Returns:
            ``None`` when the artifact is absent, malformed or newer than ``as_of``。
        """
        for path in self._candidates(model, horizon):
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            try:
                series = PredictionSeries(
                    model=str(payload.get("model", model)),
                    model_version=str(payload.get("model_version", "unknown")),
                    as_of=datetime.fromisoformat(str(payload["as_of"])),
                    anchor_at=datetime.fromisoformat(str(payload["anchor_at"])),
                    source_path=str(path),
                    points=[
                        PredictionPoint(
                            horizon=int(point["horizon"]),
                            target_at=datetime.fromisoformat(str(point["target_at"])),
                            water_level_m=(
                                None
                                if point.get("water_level_m") is None
                                else float(point["water_level_m"])
                            ),
                            source="artifact",
                        )
                        for point in payload.get("points", [])
                    ],
                )
            except (KeyError, TypeError, ValueError, ValidationError):
                continue
            if series.as_of > as_of:
                continue
            wanted = [point for point in series.points if point.horizon == horizon]
            if not wanted:
                continue
            return PredictionSeries(
                model=series.model,
                model_version=series.model_version,
                as_of=series.as_of,
                anchor_at=series.anchor_at,
                source_path=series.source_path,
                points=wanted,
            )
        return None

    def load_comparison(self) -> dict | None:
        """Load the optional ``model_comparison.json`` artifact (``None`` when absent)."""
        path = self._settings.comparison_artifact_path
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

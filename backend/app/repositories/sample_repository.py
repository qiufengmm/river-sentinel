"""Read-only repository over the small water-level sample (DS-8A).

只读取 ``Settings.resolved_sample_path``（默认 ``data/samples/water_level_sample.csv``）。
该样例是**合成演示数据**，刻意保留了三类质量缺陷（重复 ``z_id``、缺失水位、非法时间戳），
仓库负责把它们剔除并记录，绝不改写原始文件。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from ..config import Settings
from ..errors import DataUnavailableError, InvalidRequestError
from ..schemas.domain import WaterLevelPoint
from ..timeutils import ensure_aware

#: 沿用温州市公共数据开放平台数据集 12720 的字段名。
TIME_COLUMN = "time_d"
LEVEL_COLUMN = "up_water_level"
RECORD_COLUMN = "z_id"
REQUIRED_COLUMNS: tuple[str, ...] = (TIME_COLUMN, LEVEL_COLUMN, RECORD_COLUMN)


class SampleRepository:
    """Loads and normalizes the sample file into time-ordered observations."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._frame: pd.DataFrame | None = None
        self._quality_notes: list[str] = []

    @property
    def settings(self) -> Settings:
        """Settings bound to this repository."""
        return self._settings

    @property
    def quality_notes(self) -> list[str]:
        """Data-quality notes produced while normalizing the sample."""
        self.load()
        return list(self._quality_notes)

    def load(self) -> pd.DataFrame:
        """Read and normalize the sample file (cached per repository instance)."""
        if self._frame is not None:
            return self._frame

        path = self._settings.resolved_sample_path
        if not path.is_file():
            raise DataUnavailableError(f"样例数据不可用（缺失文件）：{path}")

        try:
            raw = pd.read_csv(path)
        except (OSError, ValueError, pd.errors.ParserError) as error:  # pragma: no cover
            raise DataUnavailableError(f"样例数据读取失败：{path}（{error}）") from error

        missing = [column for column in REQUIRED_COLUMNS if column not in raw.columns]
        if missing:
            raise DataUnavailableError(f"样例数据缺少必需列：{', '.join(missing)}")

        stamps = pd.to_datetime(raw[TIME_COLUMN], errors="coerce")
        levels = pd.to_numeric(raw[LEVEL_COLUMN], errors="coerce")

        if stamps.dt.tz is None:
            stamps = stamps.dt.tz_localize(self._settings.timezone)

        valid_stamps = stamps.notna()
        valid_levels = levels.notna() & np.isfinite(
            levels.to_numpy(dtype="float64", na_value=np.nan)
        )
        usable = valid_stamps & valid_levels

        frame = pd.DataFrame(
            {
                "observed_at": stamps[usable],
                "water_level_m": levels[usable].astype("float64"),
            }
        )
        duplicate_stamps = int(frame["observed_at"].duplicated().sum())
        frame = (
            frame.sort_values("observed_at")
            .drop_duplicates(subset="observed_at", keep="last")
            .reset_index(drop=True)
        )

        self._quality_notes = [
            f"样例总行数：{len(raw)}；可用观测：{len(frame)}",
            f"剔除非法时间戳行：{int((~valid_stamps).sum())}",
            f"剔除缺失或非有限水位行：{int((valid_stamps & ~valid_levels).sum())}",
            f"剔除重复时间戳行：{duplicate_stamps}",
            "样例为合成演示数据，非真实观测，不得作为实测或防汛依据。",
        ]
        self._frame = frame
        return frame

    def history(
        self,
        station_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[WaterLevelPoint]:
        """Return observations of ``station_id`` in ``[start, end]``, time ascending.

        Raises:
            DataUnavailableError: 样例文件缺失或列缺失。
            InvalidRequestError: 断面未知或时间范围倒置。
        """
        if station_id != self._settings.station_id:
            raise InvalidRequestError(
                f"未知断面 {station_id!r}；本仓库只服务 {self._settings.station_id!r}"
            )
        if start is not None and end is not None and start > end:
            raise InvalidRequestError(
                f"时间范围倒置：start={start.isoformat()} 晚于 end={end.isoformat()}"
            )

        frame = self.load()
        mask = pd.Series(True, index=frame.index)
        if start is not None:
            mask &= frame["observed_at"] >= start
        if end is not None:
            mask &= frame["observed_at"] <= end
        selected = frame.loc[mask]

        return [
            WaterLevelPoint(
                station_id=station_id,
                observed_at=ensure_aware(row.observed_at.to_pydatetime(), self._settings.timezone),
                water_level_m=float(row.water_level_m),
            )
            for row in selected.itertuples()
        ]

    def level_series(self, until: datetime | None = None) -> pd.Series:
        """Observations as a unique, monotonically increasing series indexed by time."""
        frame = self.load()
        if until is not None:
            frame = frame.loc[frame["observed_at"] <= until]
        if frame.empty:
            return pd.Series(dtype="float64")
        series = pd.Series(
            frame["water_level_m"].to_numpy(dtype="float64"),
            index=pd.DatetimeIndex(frame["observed_at"]),
            name="water_level_m",
        )
        return series.sort_index()

    def last_observed_at(self, until: datetime | None = None) -> datetime | None:
        """Latest usable observation time (``None`` when the sample has no data)."""
        series = self.level_series(until)
        if series.empty:
            return None
        return ensure_aware(series.index[-1].to_pydatetime(), self._settings.timezone)

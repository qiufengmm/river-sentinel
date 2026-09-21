"""Read-only client for the Open-Meteo Historical Weather API (hourly rainfall).

用途：为文成县目标断面批量获取**原始小时级**降雨，供后续任一预测尺度（日 / 6 小时 /
小时）自由重采样。本模块**不做任何聚合、重采样、滚动累计或特征工程**。

数据口径（必须随数据一起向下传递，不得省略）：

- Open-Meteo Historical Weather API 返回的是 **Best Match 网格化再分析降雨**（本模块默认
  **不发送** `models` 查询参数，即使用 API 默认的最优模型组合；**响应不声明**具体构成模型，
  故写「待确认」）；它**不是**文成县地面雨量站实测值，不能作为地面雨量站观测的等价替代，
  **也不得**表述为「ERA5-Land 实测」或「ERA5 实测」；
- 多点区域平均只能作为「文成县区域降雨代理变量」，本轮**不做平均**；
- 数据模型与网格分辨率**以响应原文为准**，响应未声明时写「待确认」，不得假定；
- 本数据仅用于教学科研与辅助分析，**不替代**水行政主管部门的正式监测、预警、调度或
  应急决策。

契约参数名修正（2026-09-21 实测）：Open-Meteo 实际生效的查询参数是 **`models`（复数）**，
本仓库早前冻结的 `model`（单数）会被 API **静默忽略**；本模块统一使用 `models`。

该 API 无需凭据，因此本模块不接收、不存储、不拼接任何密钥；异常消息只报异常类型与
字段名，绝不拼接完整 URL。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import datetime
from pathlib import Path
from typing import Final, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pandas as pd
from pytz.exceptions import AmbiguousTimeError, NonExistentTimeError

from river_sentinel_ml.ingestion import open_meteo_snapshots

DEFAULT_BASE_URL: Final[str] = "https://archive-api.open-meteo.com/v1/archive"
SOURCE_NAME: Final[str] = "Open-Meteo Historical Weather API"
SOURCE_URI: Final[str] = DEFAULT_BASE_URL
DATASET_ID: Final[str] = open_meteo_snapshots.DATASET_ID
DEFAULT_TIMEZONE: Final[str] = "Asia/Shanghai"
DEFAULT_VARIABLES: Final[tuple[str, ...]] = ("precipitation", "rain")

# 规范化帧只暴露这两个降雨量：其它小时变量不在本轮契约内，一律拒绝。
SUPPORTED_VARIABLES: Final[frozenset[str]] = frozenset({"precipitation", "rain"})
VARIABLE_COLUMNS: Final[Mapping[str, str]] = {
    "precipitation": "precipitation_mm",
    "rain": "rain_mm",
}
FRAME_COLUMNS: Final[tuple[str, ...]] = (
    "point_id",
    "longitude",
    "latitude",
    "observed_at",
    "precipitation_mm",
    "rain_mm",
)
REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0

_LONGITUDE_LIMIT: Final[float] = 180.0
_LATITUDE_LIMIT: Final[float] = 90.0
_DECLARED_MODEL_KEYS: Final[tuple[str, ...]] = (
    "model",
    "models",
    "resolution",
    "grid_resolution",
)
# 只有这些键会真正进入查询串；``models_explicit`` 只是本仓库的清单标记。
# 注意：Open-Meteo 生效的参数名是 ``models``（复数）；``model``（单数）会被静默忽略。
_QUERY_PARAMETER_KEYS: Final[tuple[str, ...]] = (
    "latitude",
    "longitude",
    "start_date",
    "end_date",
    "hourly",
    "timezone",
    "models",
)
# pandas 本地化/转换在不同后端下抛出的异常类型：统一收敛为 RainfallFetchError。
_LOCALIZE_ERRORS: Final[tuple[type[Exception], ...]] = (
    TypeError,
    KeyError,
    ValueError,
    AmbiguousTimeError,
    NonExistentTimeError,
)
# 已登记时区的期望 UTC 偏移（秒），用于与响应 ``utc_offset_seconds`` 交叉校验。
_EXPECTED_UTC_OFFSET_SECONDS: Final[Mapping[str, int]] = {"Asia/Shanghai": 28800}


class RainfallFetchError(RuntimeError):
    """Raised when the Open-Meteo archive endpoint cannot be read or is unusable."""


@dataclass(frozen=True)
class GridPoint:
    """One frozen grid point of the Wencheng county rainfall proxy network."""

    point_id: str
    longitude: float
    latitude: float


@dataclass(frozen=True)
class RainfallProvenance:
    """Source traceability for one fetch of raw hourly rainfall."""

    source_name: str = SOURCE_NAME
    source_uri: str = SOURCE_URI
    request_parameters: Mapping[str, object] = field(default_factory=dict)
    acquired_at: datetime | None = None
    record_count: int = 0
    sha256: str = ""
    dataset_notes: str = ""


@dataclass(frozen=True)
class HourlyRainfall:
    """Raw hourly rainfall plus its provenance and optional snapshot locations."""

    frame: pd.DataFrame
    provenance: RainfallProvenance
    raw_response_path: Path | None = None
    raw_response_paths: tuple[Path, ...] = ()


def _point_value(point: object, name: str) -> object:
    """Read a grid-point field from a mapping, a dataclass or any attribute holder."""
    if isinstance(point, Mapping):
        return point.get(name)
    return getattr(point, name, None)


def _validated_points(points: Iterable[object]) -> tuple[GridPoint, ...]:
    """Validate and normalize the requested grid points."""
    candidates = list(points)
    if not candidates:
        raise ValueError("points must not be empty")

    normalized: list[GridPoint] = []
    for index, point in enumerate(candidates):
        point_id = _point_value(point, "point_id")
        if not isinstance(point_id, str) or not point_id.strip():
            raise ValueError(f"points[{index}].point_id must be a non-empty string")

        longitude = _point_value(point, "longitude")
        if isinstance(longitude, bool) or not isinstance(longitude, int | float):
            raise ValueError(f"points[{index}].longitude must be a number")
        longitude = float(longitude)
        if not math.isfinite(longitude):
            raise ValueError(f"points[{index}].longitude must be a finite number")
        if abs(longitude) > _LONGITUDE_LIMIT:
            raise ValueError(
                f"points[{index}].longitude must be within "
                f"[-{_LONGITUDE_LIMIT}, {_LONGITUDE_LIMIT}], got {longitude}"
            )

        latitude = _point_value(point, "latitude")
        if isinstance(latitude, bool) or not isinstance(latitude, int | float):
            raise ValueError(f"points[{index}].latitude must be a number")
        latitude = float(latitude)
        if not math.isfinite(latitude):
            raise ValueError(f"points[{index}].latitude must be a finite number")
        if abs(latitude) > _LATITUDE_LIMIT:
            raise ValueError(
                f"points[{index}].latitude must be within "
                f"[-{_LATITUDE_LIMIT}, {_LATITUDE_LIMIT}], got {latitude}"
            )

        normalized.append(GridPoint(point_id=point_id, longitude=longitude, latitude=latitude))
    return tuple(normalized)


def _validated_date(value: object, field_name: str) -> str:
    """Normalize a date argument to an ISO ``YYYY-MM-DD`` string."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date_type):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a date or an ISO date string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must not be empty")
    try:
        return date_type.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name} must use the ISO format YYYY-MM-DD, got {value!r}") from exc


def _validated_variables(variables: Iterable[str]) -> tuple[str, ...]:
    """Validate the requested hourly variables; only rainfall variables are supported."""
    requested = tuple(variables)
    if not requested:
        raise ValueError("variables must not be empty")
    unknown = [name for name in requested if name not in SUPPORTED_VARIABLES]
    if unknown:
        supported = ", ".join(sorted(SUPPORTED_VARIABLES))
        raise ValueError(
            f"variables must be a subset of {{{supported}}}: unsupported {', '.join(unknown)}"
        )
    if len(set(requested)) != len(requested):
        raise ValueError("variables must not contain duplicates")
    return requested


def _validated_timezone(timezone: str) -> str:
    """Validate the IANA time zone name up front so localize failures stay explainable."""
    if not isinstance(timezone, str) or not timezone.strip():
        raise ValueError("timezone must not be empty")
    text = timezone.strip()
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"timezone must be a valid IANA time zone name, got {timezone!r}") from exc
    return text


def _validated_models(models: str | None) -> str | None:
    """``None`` means "do not send ``models``" (the API default Best Match)."""
    if models is None:
        return None
    if not isinstance(models, str) or not models.strip():
        raise ValueError("models must be a non-empty string when provided")
    return models.strip()


def _utc_offset_note(payload: Mapping[str, object], timezone: str) -> str:
    """Cross-check the response ``utc_offset_seconds`` against the requested time zone."""
    offset = payload.get("utc_offset_seconds")
    expected = _EXPECTED_UTC_OFFSET_SECONDS.get(timezone)
    if isinstance(offset, bool) or not isinstance(offset, int):
        return "待确认（响应未声明 utc_offset_seconds）"
    if expected is None:
        return f"响应={offset}（时区 {timezone} 未登记期望值，待确认）"
    if offset != expected:
        return f"不一致：响应={offset}，期望 {timezone}={expected}"
    return f"一致：响应={offset}，期望 {timezone}={expected}"


def _empty_frame() -> pd.DataFrame:
    """An empty frame that still carries the full contracted dtype set."""
    return pd.DataFrame(
        {
            "point_id": pd.Series([], dtype="object"),
            "longitude": pd.Series([], dtype="float64"),
            "latitude": pd.Series([], dtype="float64"),
            "observed_at": pd.Series([], dtype=f"datetime64[ns, {DEFAULT_TIMEZONE}]"),
            "precipitation_mm": pd.Series([], dtype="float64"),
            "rain_mm": pd.Series([], dtype="float64"),
        }
    )


def _dataset_notes(
    payloads: Sequence[Mapping[str, object]],
    point_ids: Sequence[str],
    models: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
) -> str:
    """Build the dataset notes strictly from what the responses actually declare."""
    base = (
        "Open-Meteo Historical Weather API 网格化再分析降雨（Best Match 口径，"
        "响应未声明具体构成模型，待确认），非地面雨量站实测值，"
        "不能替代文成县地面雨量站观测，也不得表述为任一具体再分析数据集的实测值；"
        "多点区域平均只能作为「文成县区域降雨代理变量」（本轮不做平均）；"
        "本数据仅用于教学科研与辅助分析，不替代水行政主管部门的正式监测、预警、调度或应急决策。"
    )
    selection = (
        "模型选择[本次未发送 models 查询参数，使用 API 默认 Best Match]"
        if models is None
        else f"模型选择[本次发送 models={models!r}]"
    )
    details: list[str] = []
    for point_id, payload in zip(point_ids, payloads, strict=False):
        declared = [
            f"{key}={payload[key]!r}"
            for key in _DECLARED_MODEL_KEYS
            if key in payload and payload[key] is not None
        ]
        declared_text = "；".join(declared) if declared else "待确认（响应未声明数据模型与分辨率）"
        details.append(
            f"{point_id}: 模型/分辨率[{declared_text}]；"
            f"时区偏移[{_utc_offset_note(payload, timezone)}]；"
            f"网格单元[latitude={payload.get('latitude')!r}, "
            f"longitude={payload.get('longitude')!r}, elevation={payload.get('elevation')!r}]"
        )
    return f"{base} {selection}；实测：{' | '.join(details)}"


class OpenMeteoClient:
    """Thin read-only adapter over the Open-Meteo Historical Weather API."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def fetch_hourly_rainfall(
        self,
        points: Iterable[object],
        start_date: object,
        end_date: object,
        *,
        timezone: str = DEFAULT_TIMEZONE,
        variables: Iterable[str] = DEFAULT_VARIABLES,
        models: str | None = None,
        snapshot_root: Path | None = None,
    ) -> HourlyRainfall:
        """Fetch raw hourly rainfall for every requested grid point.

        ``observed_at`` is tz-aware and strictly increasing **within each point**,
        kept at the API's original hourly resolution: no aggregation, no resampling,
        no interpolation is performed here. Multi-point frames are grouped by
        ``point_id`` in the requested order.

        ``models`` is sent as the ``models`` query parameter (the name Open-Meteo
        actually honours); ``None`` means the parameter is **not sent at all**, i.e.
        the API default Best Match.

        ``snapshot_root`` is an additive keyword: when given, every point gets its own
        exclusive raw-response snapshot; when omitted no file is written and
        ``raw_response_path`` is ``None``. Snapshots are written only **after** the
        response has been validated, so a rejected response never leaves a snapshot.
        """
        normalized_points = _validated_points(points)
        start = _validated_date(start_date, "start_date")
        end = _validated_date(end_date, "end_date")
        if start > end:
            raise ValueError(f"start_date must not be later than end_date: {start} > {end}")
        zone = _validated_timezone(timezone)
        requested_variables = _validated_variables(variables)
        model_name = _validated_models(models)

        acquired_at = open_meteo_snapshots.acquired_now()
        frames: list[pd.DataFrame] = []
        payloads: list[Mapping[str, object]] = []
        snapshot_paths: list[Path] = []
        canonical_chunks: list[bytes] = []

        for point in normalized_points:
            parameters = self._request_parameters(
                point, start, end, zone, requested_variables, model_name
            )
            payload = self._request_json(self._query_parameters(parameters))
            payloads.append(payload)
            canonical = open_meteo_snapshots.canonical_json_bytes(payload)
            canonical_chunks.append(canonical)
            # 先解析校验、后落盘：不合规响应绝不产生快照残骸。
            point_frame = self._point_frame(point, payload, requested_variables, zone)

            if snapshot_root is not None:
                metadata = open_meteo_snapshots.RainfallSnapshotMetadata(
                    source_name=SOURCE_NAME,
                    source_uri=SOURCE_URI,
                    dataset_id=DATASET_ID,
                    point_id=point.point_id,
                    start_date=start,
                    end_date=end,
                    request_parameters=parameters,
                )
                snapshot_path, _, _ = open_meteo_snapshots.write_hourly_snapshot(
                    payload,
                    metadata,
                    Path(snapshot_root),
                    record_count=self._record_count(payload),
                    acquired_at=acquired_at,
                )
                snapshot_paths.append(snapshot_path)

            frames.append(point_frame)

        frame = pd.concat(frames, ignore_index=True) if frames else _empty_frame()
        frame = frame.loc[:, list(FRAME_COLUMNS)]

        provenance = RainfallProvenance(
            source_name=SOURCE_NAME,
            source_uri=SOURCE_URI,
            request_parameters=self._provenance_parameters(
                normalized_points, start, end, zone, requested_variables, model_name
            ),
            acquired_at=acquired_at,
            record_count=int(len(frame)),
            sha256=hashlib.sha256(b"".join(canonical_chunks)).hexdigest(),
            dataset_notes=_dataset_notes(
                payloads,
                [point.point_id for point in normalized_points],
                model_name,
                zone,
            ),
        )
        return HourlyRainfall(
            frame=frame,
            provenance=provenance,
            raw_response_path=snapshot_paths[0] if snapshot_paths else None,
            raw_response_paths=tuple(snapshot_paths),
        )

    @classmethod
    def _request_parameters(
        cls,
        point: GridPoint,
        start: str,
        end: str,
        timezone: str,
        variables: Sequence[str],
        models: str | None,
    ) -> dict[str, str | int | bool | None]:
        parameters: dict[str, str | int | bool | None] = {
            "latitude": f"{point.latitude}",
            "longitude": f"{point.longitude}",
            "start_date": start,
            "end_date": end,
            "hourly": ",".join(variables),
            "timezone": timezone,
            # 是否显式传 models 单独占一个键：清单必须能区分「未传默认值」与「显式传值」。
            "models": models,
            "models_explicit": models is not None,
        }
        # 清单必须能回答「本次到底发了什么」：把实际进入查询串的键值原样留痕。
        parameters["sent_query_parameters"] = json.dumps(
            cls._query_parameters(parameters), sort_keys=True, ensure_ascii=False
        )
        return parameters

    @staticmethod
    def _provenance_parameters(
        points: Sequence[GridPoint],
        start: str,
        end: str,
        timezone: str,
        variables: Sequence[str],
        models: str | None,
    ) -> dict[str, str | int | bool | None]:
        parameters: dict[str, str | int | bool | None] = {
            "start_date": start,
            "end_date": end,
            "hourly": ",".join(variables),
            "timezone": timezone,
            "models": models,
            "models_explicit": models is not None,
            "model_selection": "api_default_best_match" if models is None else "explicit_models",
            "point_count": len(points),
            "points": ",".join(
                f"{point.point_id}@{point.longitude},{point.latitude}" for point in points
            ),
        }
        return parameters

    @staticmethod
    def _query_parameters(parameters: Mapping[str, object]) -> dict[str, str]:
        """Project the manifest parameters onto the keys the archive endpoint accepts."""
        return {
            key: str(parameters[key])
            for key in _QUERY_PARAMETER_KEYS
            if parameters.get(key) is not None
        }

    @staticmethod
    def _record_count(payload: Mapping[str, object]) -> int:
        hourly = payload.get("hourly")
        if isinstance(hourly, Mapping) and isinstance(hourly.get("time"), list):
            return len(hourly["time"])
        return 0

    def _request_json(self, parameters: Mapping[str, object]) -> Mapping[str, object]:
        """Issue one archive request and return the decoded JSON object."""
        query = {key: value for key, value in parameters.items() if value is not None}
        try:
            response = self._client.get(self._base_url, params=query)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            # 只报异常类型：httpx 的错误文本可能携带完整 URL，本模块绝不外泄。
            raise RainfallFetchError(
                f"open-meteo archive request failed: {type(exc).__name__}"
            ) from exc
        except httpx.InvalidURL as exc:
            # InvalidURL 不是 HTTPError，但同样属于「请求发不出去」，必须收敛成同一异常类型。
            raise RainfallFetchError(
                f"open-meteo archive request failed: {type(exc).__name__}"
            ) from exc
        except ValueError as exc:
            raise RainfallFetchError(
                "open-meteo archive request failed: response is not valid JSON"
            ) from exc

        if not isinstance(payload, Mapping):
            raise RainfallFetchError(
                f"open-meteo archive response must be a JSON object, got {type(payload).__name__}"
            )
        return payload

    def _point_frame(
        self,
        point: GridPoint,
        payload: Mapping[str, object],
        variables: Sequence[str],
        timezone: str,
    ) -> pd.DataFrame:
        hourly = payload.get("hourly")
        if not isinstance(hourly, Mapping):
            raise RainfallFetchError(
                "open-meteo archive response is missing the required 'hourly' field"
            )
        times = hourly.get("time")
        if not isinstance(times, list):
            raise RainfallFetchError(
                "open-meteo archive response is missing the required 'time' field"
            )

        observed_at = self._observed_at(times, timezone)
        count = len(times)
        columns: dict[str, pd.Series] = {
            "point_id": pd.Series([point.point_id] * count, dtype="object"),
            "longitude": pd.Series([point.longitude] * count, dtype="float64"),
            "latitude": pd.Series([point.latitude] * count, dtype="float64"),
            "observed_at": observed_at,
        }
        for variable in SUPPORTED_VARIABLES:
            column = VARIABLE_COLUMNS[variable]
            if variable in variables:
                columns[column] = self._millimetre_series(hourly, variable, count)
            else:
                columns[column] = pd.Series([float("nan")] * count, dtype="float64")
        return pd.DataFrame(columns)

    @staticmethod
    def _observed_at(times: Sequence[object], timezone: str) -> pd.Series:
        """Parse the API timestamps into a strictly increasing tz-aware series.

        响应自带 UTC 偏移时走 ``tz_convert``（转换），否则按请求时区 ``tz_localize``（本地化）；
        两种路径的后端异常统一收敛为 ``RainfallFetchError``，消息不含 URL 与凭据。
        """
        parsed = pd.to_datetime(
            pd.Series(list(times), dtype="object"), format="ISO8601", errors="coerce"
        )
        if parsed.isna().any():
            raise RainfallFetchError(
                "open-meteo archive response contains an unparseable 'time' value"
            )
        if parsed.dtype == object:
            raise RainfallFetchError(
                "open-meteo archive response 'time' values must share one UTC offset"
            )
        try:
            if parsed.dt.tz is not None:
                localized = parsed.dt.tz_convert(timezone)
            else:
                localized = parsed.dt.tz_localize(timezone, ambiguous="raise", nonexistent="raise")
        except _LOCALIZE_ERRORS as exc:
            raise RainfallFetchError(
                "open-meteo archive response 'time' values cannot be resolved to the requested "
                f"timezone {timezone!r}: {type(exc).__name__}"
            ) from exc
        if not localized.is_monotonic_increasing or not localized.is_unique:
            raise RainfallFetchError(
                "open-meteo archive response 'time' values must be strictly increasing"
            )
        return localized

    @staticmethod
    def _millimetre_series(hourly: Mapping[str, object], variable: str, count: int) -> pd.Series:
        values = hourly.get(variable)
        if not isinstance(values, list):
            raise RainfallFetchError(
                f"open-meteo archive response is missing the required hourly field {variable!r}"
            )
        if len(values) != count:
            raise RainfallFetchError(
                f"open-meteo archive response field {variable!r} has {len(values)} values, "
                f"expected {count}"
            )
        return pd.to_numeric(pd.Series(values, dtype="object"), errors="coerce").astype("float64")

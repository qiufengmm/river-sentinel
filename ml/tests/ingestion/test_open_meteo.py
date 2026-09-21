"""Contract tests for the Open-Meteo historical hourly rainfall client.

本文件只覆盖「原始小时降雨获取 + 不可变快照 + 小时表落盘」的契约，
**不含**任何日聚合、滚动累计、滞后或特征工程断言（本轮范围明确排除）。

数据口径提醒：Open-Meteo Historical Weather API 返回的是 **Best Match 网格化再分析降雨**
（默认不发送 `models` 查询参数，响应不声明具体构成模型），不是文成县地面雨量站实测值，
**也不得**表述为「ERA5-Land 实测」；多点区域平均只能作为「文成县区域降雨代理变量」。
本系统仅用于教学科研与辅助分析，不替代水行政主管部门的正式监测、预警、调度或应急决策。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd
import pytest

from river_sentinel_ml.ingestion import open_meteo_snapshots
from river_sentinel_ml.ingestion.open_meteo import (
    DEFAULT_BASE_URL,
    GridPoint,
    HourlyRainfall,
    OpenMeteoClient,
    RainfallFetchError,
)

WC_G1 = GridPoint(point_id="WC-G1", longitude=120.03, latitude=27.79)
WC_G2 = GridPoint(point_id="WC-G2", longitude=119.85, latitude=27.92)


def _offline_transport() -> httpx.MockTransport:
    """A transport that fails the test instead of reaching the network.

    参数校验类用例绝不允许发起真实请求：一旦将来校验顺序变化导致请求被发出，测试立即失败。
    """

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        pytest.fail(f"unexpected network request: {request.url}")

    return httpx.MockTransport(handler)


def _hourly_payload(
    times: list[str],
    precipitation: list[float | None],
    rain: list[float | None],
    *,
    latitude: float = 27.732864,
    longitude: float = 120.03371,
) -> dict[str, object]:
    """Build a response shaped like the Open-Meteo archive endpoint output."""
    return {
        "latitude": latitude,
        "longitude": longitude,
        "generationtime_ms": 0.51,
        "utc_offset_seconds": 28800,
        "timezone": "Asia/Shanghai",
        "timezone_abbreviation": "GMT+8",
        "elevation": 267.0,
        "hourly_units": {"time": "iso8601", "precipitation": "mm", "rain": "mm"},
        "hourly": {
            "time": list(times),
            "precipitation": list(precipitation),
            "rain": list(rain),
        },
    }


def test_normal_response_is_parsed_with_units_timezone_and_hour_precision() -> None:
    times = ["2015-01-01T00:00", "2015-01-01T01:00", "2015-01-01T02:00"]
    payload = _hourly_payload(times, [0.0, 1.5, 2.5], [0.0, 1.5, 2.0])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-03")

    assert isinstance(result, HourlyRainfall)
    frame = result.frame
    assert list(frame.columns) == [
        "point_id",
        "longitude",
        "latitude",
        "observed_at",
        "precipitation_mm",
        "rain_mm",
    ]
    assert frame["point_id"].tolist() == ["WC-G1", "WC-G1", "WC-G1"]
    assert frame["longitude"].tolist() == [120.03, 120.03, 120.03]
    assert frame["latitude"].tolist() == [27.79, 27.79, 27.79]
    assert frame["precipitation_mm"].tolist() == [0.0, 1.5, 2.5]
    assert frame["rain_mm"].tolist() == [0.0, 1.5, 2.0]
    assert str(frame["observed_at"].dt.tz) == "Asia/Shanghai"
    assert frame["observed_at"].iloc[0] == pd.Timestamp("2015-01-01T00:00:00+08:00")
    assert frame["observed_at"].iloc[-1] == pd.Timestamp("2015-01-01T02:00:00+08:00")
    assert (frame["observed_at"].dt.minute == 0).all()
    assert (frame["observed_at"].dt.second == 0).all()
    assert result.provenance.record_count == 3
    assert result.provenance.source_uri == DEFAULT_BASE_URL
    assert "非地面雨量站实测值" in result.provenance.dataset_notes


def test_multiple_points_keep_their_point_id_and_increasing_timestamps() -> None:
    times = ["2015-01-01T00:00", "2015-01-01T01:00"]

    def handler(request: httpx.Request) -> httpx.Response:
        latitude = float(request.url.params["latitude"])
        if latitude == 27.79:
            return httpx.Response(
                200, json=_hourly_payload(times, [0.0, 1.0], [0.0, 1.0], latitude=27.732864)
            )
        return httpx.Response(
            200, json=_hourly_payload(times, [2.0, 3.0], [2.0, 3.0], latitude=27.921)
        )

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_hourly_rainfall([WC_G1, WC_G2], "2015-01-01", "2015-01-03")

    frame = result.frame
    assert len(frame) == 4
    assert frame["point_id"].tolist() == ["WC-G1", "WC-G1", "WC-G2", "WC-G2"]
    assert frame["precipitation_mm"].tolist() == [0.0, 1.0, 2.0, 3.0]
    assert str(frame["observed_at"].dt.tz) == "Asia/Shanghai"
    for point_id in ("WC-G1", "WC-G2"):
        series = frame.loc[frame["point_id"] == point_id, "observed_at"]
        assert series.is_monotonic_increasing
        assert series.is_unique
        assert series.diff().dropna().eq(pd.Timedelta(hours=1)).all()
        assert series.dt.tz is not None


def test_missing_hourly_section_is_rejected() -> None:
    payload = {"latitude": 27.79, "longitude": 120.03}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RainfallFetchError, match="hourly"):
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-03")


def test_missing_time_field_is_rejected() -> None:
    payload = _hourly_payload(["2015-01-01T00:00"], [0.0], [0.0])
    del payload["hourly"]["time"]  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RainfallFetchError, match="time"):
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-03")


def test_non_json_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RainfallFetchError, match="JSON"):
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-03")


def test_http_error_is_reported_without_the_request_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server exploded")

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RainfallFetchError) as error:
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-03")

    message = str(error.value)
    assert "https://" not in message
    assert "archive-api.open-meteo.com" not in message
    assert "server exploded" not in message


def test_timeout_is_reported_without_the_request_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("network timeout", request=request)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RainfallFetchError) as error:
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-03")

    message = str(error.value)
    assert "https://" not in message
    assert "network timeout" not in message


def test_models_query_parameter_is_named_models_and_absent_when_omitted() -> None:
    """契约修订证据：实际发送的是 ``models``（复数），``model``（单数）绝不再出现。"""
    payload = _hourly_payload(["2015-01-01T00:00"], [0.0], [0.0])
    captured: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url)
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")
        first = captured[-1]
        assert "models" not in first.params
        assert "model" not in first.params
        assert first.params["hourly"] == "precipitation,rain"
        assert first.params["timezone"] == "Asia/Shanghai"

        client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02", models="era5")
        second = captured[-1]
        assert second.params["models"] == "era5"
        assert "model" not in second.params

    assert "models=era5" in str(captured[-1])
    assert "model=" not in str(captured[0])


def test_start_date_after_end_date_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be issued")

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="start_date"):
            client.fetch_hourly_rainfall([WC_G1], "2015-01-03", "2015-01-01")


def test_out_of_range_longitude_is_rejected() -> None:
    with OpenMeteoClient(transport=_offline_transport()) as client:
        with pytest.raises(ValueError, match="longitude"):
            client.fetch_hourly_rainfall(
                [GridPoint("WC-BAD", 200.0, 27.79)], "2015-01-01", "2015-01-02"
            )


def test_out_of_range_latitude_is_rejected() -> None:
    with OpenMeteoClient(transport=_offline_transport()) as client:
        with pytest.raises(ValueError, match="latitude"):
            client.fetch_hourly_rainfall(
                [GridPoint("WC-BAD", 120.03, 95.0)], "2015-01-01", "2015-01-02"
            )


def test_non_finite_coordinates_are_rejected() -> None:
    with OpenMeteoClient(transport=_offline_transport()) as client:
        with pytest.raises(ValueError, match="longitude"):
            client.fetch_hourly_rainfall(
                [GridPoint("WC-BAD", float("nan"), 27.79)], "2015-01-01", "2015-01-02"
            )
        with pytest.raises(ValueError, match="latitude"):
            client.fetch_hourly_rainfall(
                [GridPoint("WC-BAD", 120.03, float("inf"))], "2015-01-01", "2015-01-02"
            )


def test_empty_points_are_rejected() -> None:
    with OpenMeteoClient(transport=_offline_transport()) as client:
        with pytest.raises(ValueError, match="points"):
            client.fetch_hourly_rainfall([], "2015-01-01", "2015-01-02")


def test_empty_variables_are_rejected() -> None:
    with OpenMeteoClient(transport=_offline_transport()) as client:
        with pytest.raises(ValueError, match="variables"):
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02", variables=())


def test_unknown_variables_are_rejected() -> None:
    with OpenMeteoClient(transport=_offline_transport()) as client:
        with pytest.raises(ValueError, match="variables"):
            client.fetch_hourly_rainfall(
                [WC_G1], "2015-01-01", "2015-01-02", variables=("temperature_2m",)
            )


def test_blank_models_is_rejected() -> None:
    with OpenMeteoClient(transport=_offline_transport()) as client:
        with pytest.raises(ValueError, match="models"):
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02", models="")


def test_empty_timezone_is_rejected() -> None:
    with OpenMeteoClient(transport=_offline_transport()) as client:
        with pytest.raises(ValueError, match="timezone"):
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02", timezone="")


def test_unknown_timezone_is_rejected() -> None:
    with OpenMeteoClient(transport=_offline_transport()) as client:
        with pytest.raises(ValueError, match="timezone"):
            client.fetch_hourly_rainfall(
                [WC_G1], "2015-01-01", "2015-01-02", timezone="Mars/Olympus_Mons"
            )


def test_snapshot_is_written_with_a_matching_sha256(tmp_path: Path, monkeypatch) -> None:
    fixed = datetime(2026, 9, 21, 3, 0, tzinfo=UTC)
    monkeypatch.setattr(open_meteo_snapshots, "acquired_now", lambda: fixed)
    payload = _hourly_payload(["2015-01-01T00:00", "2015-01-01T01:00"], [0.0, 1.0], [0.0, 1.0])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_hourly_rainfall(
            [WC_G1], "2015-01-01", "2015-01-03", snapshot_root=tmp_path
        )

    assert result.raw_response_path is not None
    snapshot_path = result.raw_response_path
    assert snapshot_path.parent.name == "2015-01-01_2015-01-03"
    assert snapshot_path.parent.parent.name == "WC-G1"
    assert snapshot_path.parent.parent.parent.name == "open-meteo"

    raw_bytes = snapshot_path.read_bytes()
    assert hashlib.sha256(raw_bytes).hexdigest() == result.provenance.sha256
    json.loads(raw_bytes.decode("utf-8"))

    manifest_path = snapshot_path.parent / f"{snapshot_path.stem}.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["point_id"] == "WC-G1"
    assert manifest["record_count"] == 2
    assert manifest["sha256"] == hashlib.sha256(raw_bytes).hexdigest()
    assert manifest["request_parameters"]["models_explicit"] is False
    assert manifest["request_parameters"]["models"] is None
    # 清单必须留痕「本次到底发了哪些查询参数」：未显式指定时不能出现 models。
    sent = json.loads(manifest["request_parameters"]["sent_query_parameters"])
    assert sent["timezone"] == "Asia/Shanghai"
    assert sent["hourly"] == "precipitation,rain"
    assert "models" not in sent


def test_snapshot_is_never_overwritten(tmp_path: Path, monkeypatch) -> None:
    fixed = datetime(2026, 9, 21, 3, 0, tzinfo=UTC)
    monkeypatch.setattr(open_meteo_snapshots, "acquired_now", lambda: fixed)
    payload = _hourly_payload(["2015-01-01T00:00"], [0.0], [0.0])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        first = client.fetch_hourly_rainfall(
            [WC_G1], "2015-01-01", "2015-01-03", snapshot_root=tmp_path
        )
        first_bytes = first.raw_response_path.read_bytes()  # type: ignore[union-attr]
        with pytest.raises(FileExistsError):
            client.fetch_hourly_rainfall(
                [WC_G1], "2015-01-01", "2015-01-03", snapshot_root=tmp_path
            )

    assert first.raw_response_path.read_bytes() == first_bytes  # type: ignore[union-attr]


def test_hourly_table_is_written_without_any_aggregation(tmp_path: Path) -> None:
    times = [f"2015-01-0{day}T{hour:02d}:00" for day in (1, 2, 3) for hour in range(24)]
    payload = _hourly_payload(times, [0.1] * 72, [0.1] * 72)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-03")

    path = open_meteo_snapshots.write_hourly_table(
        result.frame, "WC-G1", "2015-01-01", "2015-01-03", tmp_path
    )
    assert path == tmp_path / "open-meteo" / "hourly" / "WC-G1" / "2015-01-01_2015-01-03.csv"

    table = pd.read_csv(path)
    assert list(table.columns) == [
        "observed_at",
        "precipitation_mm",
        "rain_mm",
        "point_id",
        "longitude",
        "latitude",
    ]
    assert len(table) == 72
    assert table["point_id"].unique().tolist() == ["WC-G1"]
    assert table["observed_at"].iloc[0] == "2015-01-01T00:00:00+08:00"
    assert table["observed_at"].iloc[-1] == "2015-01-03T23:00:00+08:00"
    assert table["observed_at"].map(lambda value: value.endswith("+08:00")).all()
    assert len(table["observed_at"].unique()) == 72


def test_empty_payload_returns_a_complete_empty_frame() -> None:
    payload = _hourly_payload([], [], [])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-03")

    frame = result.frame
    assert frame.empty
    assert result.provenance.record_count == 0
    assert list(frame.columns) == [
        "point_id",
        "longitude",
        "latitude",
        "observed_at",
        "precipitation_mm",
        "rain_mm",
    ]
    assert str(frame["observed_at"].dtype) == "datetime64[ns, Asia/Shanghai]"
    assert str(frame["precipitation_mm"].dtype) == "float64"
    assert str(frame["rain_mm"].dtype) == "float64"
    assert str(frame["longitude"].dtype) == "float64"
    assert str(frame["latitude"].dtype) == "float64"


def test_input_arguments_are_not_mutated() -> None:
    payload = _hourly_payload(["2015-01-01T00:00"], [0.0], [0.0])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    # 用可变容器 + 副本做比较，才能真实验证「入参未被就地修改」；
    # 对不可变 tuple 做等值比较是恒真断言，没有验证力。
    points = [WC_G1, WC_G2]
    variables = ["precipitation", "rain"]
    points_snapshot = list(points)
    variables_snapshot = list(variables)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        client.fetch_hourly_rainfall(points, "2015-01-01", "2015-01-02", variables=variables)

    assert points == points_snapshot
    assert variables == variables_snapshot
    assert [item.point_id for item in points] == ["WC-G1", "WC-G2"]
    assert variables[0] == "precipitation"
    assert variables[1] == "rain"


def test_dataset_notes_state_best_match_and_deny_station_observations() -> None:
    payload = _hourly_payload(["2015-01-01T00:00"], [1.0], [1.0])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")

    notes = result.provenance.dataset_notes
    assert "Best Match" in notes
    assert "非地面雨量站实测值" in notes
    assert "本次未发送 models 查询参数，使用 API 默认 Best Match" in notes
    assert "ERA5-Land" not in notes
    assert result.provenance.request_parameters["model_selection"] == "api_default_best_match"


def test_dataset_notes_record_an_explicit_models_selection() -> None:
    payload = _hourly_payload(["2015-01-01T00:00"], [1.0], [1.0])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02", models="era5")

    notes = result.provenance.dataset_notes
    assert "本次发送 models='era5'" in notes
    assert result.provenance.request_parameters["models"] == "era5"
    assert result.provenance.request_parameters["models_explicit"] is True
    assert result.provenance.request_parameters["model_selection"] == "explicit_models"


def test_response_timestamps_carrying_their_own_offset_are_converted() -> None:
    """响应自带 UTC 偏移时走 tz_convert，而不是在 tz_localize 上报错。"""
    payload = _hourly_payload(
        ["2015-01-01T00:00+08:00", "2015-01-01T01:00+08:00"], [0.0, 1.0], [0.0, 1.0]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")

    assert str(result.frame["observed_at"].dt.tz) == "Asia/Shanghai"
    assert result.frame["observed_at"].iloc[0] == pd.Timestamp("2015-01-01T00:00:00+08:00")


def test_response_timestamps_in_another_offset_are_shifted_correctly() -> None:
    payload = _hourly_payload(
        ["2015-01-01T00:00+00:00", "2015-01-01T01:00+00:00"], [0.0, 1.0], [0.0, 1.0]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")

    assert str(result.frame["observed_at"].dt.tz) == "Asia/Shanghai"
    assert result.frame["observed_at"].iloc[0] == pd.Timestamp("2015-01-01T08:00:00+08:00")


def test_nonexistent_local_time_is_reported_as_a_fetch_error() -> None:
    """夏令时跳变时刻必须收敛为 RainfallFetchError，而不是泄漏 pandas 后端异常。"""
    payload = _hourly_payload(["2015-03-08T02:30"], [0.0], [0.0])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RainfallFetchError) as error:
            client.fetch_hourly_rainfall(
                [WC_G1], "2015-03-08", "2015-03-08", timezone="America/New_York"
            )

    assert "America/New_York" in str(error.value)
    assert "https://" not in str(error.value)


def test_non_increasing_time_is_rejected() -> None:
    payload = _hourly_payload(
        ["2015-01-01T00:00", "2015-01-01T03:00", "2015-01-01T01:00"],
        [0.0, 1.0, 2.0],
        [0.0, 1.0, 2.0],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RainfallFetchError, match="strictly increasing"):
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")


def test_duplicate_time_is_rejected() -> None:
    payload = _hourly_payload(
        ["2015-01-01T00:00", "2015-01-01T01:00", "2015-01-01T01:00"],
        [0.0, 1.0, 2.0],
        [0.0, 1.0, 2.0],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RainfallFetchError, match="strictly increasing"):
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")


def test_rainfall_field_length_mismatch_is_rejected() -> None:
    payload = _hourly_payload(["2015-01-01T00:00", "2015-01-01T01:00"], [0.0], [0.0])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RainfallFetchError, match="expected 2"):
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")


def test_json_array_response_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RainfallFetchError, match="JSON object"):
            client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")


def test_invalid_response_leaves_no_snapshot_behind(tmp_path: Path) -> None:
    """P1-2：校验必须在落盘之前；不合规响应不得留下快照残骸。"""
    payload = _hourly_payload(["2015-01-01T00:00"], [0.0], [0.0])
    del payload["hourly"]["precipitation"]  # type: ignore[index]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RainfallFetchError, match="precipitation"):
            client.fetch_hourly_rainfall(
                [WC_G1], "2015-01-01", "2015-01-02", snapshot_root=tmp_path
            )

    assert not (tmp_path / "open-meteo").exists()


def test_json_safe_replaces_non_finite_values_with_null() -> None:
    payload = {
        "a": float("nan"),
        "b": [1.0, float("inf")],
        "c": {"d": float("-inf")},
        "e": 2.0,
    }
    safe = open_meteo_snapshots._json_safe(payload)

    assert safe == {"a": None, "b": [1.0, None], "c": {"d": None}, "e": 2.0}
    # 键与容器类型保持不变，只在取值位置替换。
    assert sorted(safe) == ["a", "b", "c", "e"]
    assert isinstance(safe["b"], list)  # type: ignore[index]
    decoded = json.loads(open_meteo_snapshots.canonical_json_bytes(payload).decode("utf-8"))
    assert decoded == {"a": None, "b": [1.0, None], "c": {"d": None}, "e": 2.0}


def test_manifest_construction_failure_rolls_back_the_snapshot(tmp_path: Path, monkeypatch):
    def boom(manifest: object) -> dict[str, object]:
        raise RuntimeError("manifest construction failed")

    monkeypatch.setattr(open_meteo_snapshots, "_manifest_dict", boom)
    metadata = open_meteo_snapshots.RainfallSnapshotMetadata(
        source_name="Open-Meteo Historical Weather API",
        source_uri="https://archive-api.open-meteo.com/v1/archive",
        dataset_id="open-meteo-archive-hourly",
        point_id="WC-G1",
        start_date="2015-01-01",
        end_date="2015-01-02",
    )

    with pytest.raises(RuntimeError, match="manifest construction failed"):
        open_meteo_snapshots.write_hourly_snapshot(
            {"hourly": {"time": []}}, metadata, tmp_path, record_count=0
        )

    assert [path for path in (tmp_path / "open-meteo").rglob("*") if path.is_file()] == []


def test_manifest_write_failure_rolls_back_the_snapshot(tmp_path: Path, monkeypatch) -> None:
    real_open = open_meteo_snapshots.Path.open

    def failing_open(self: Path, mode: str = "r", *args: object, **kwargs: object):
        if "b" not in mode:
            raise OSError("disk full")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(open_meteo_snapshots.Path, "open", failing_open)
    metadata = open_meteo_snapshots.RainfallSnapshotMetadata(
        source_name="Open-Meteo Historical Weather API",
        source_uri="https://archive-api.open-meteo.com/v1/archive",
        dataset_id="open-meteo-archive-hourly",
        point_id="WC-G1",
        start_date="2015-01-01",
        end_date="2015-01-02",
    )

    with pytest.raises(OSError):
        open_meteo_snapshots.write_hourly_snapshot(
            {"hourly": {"time": []}}, metadata, tmp_path, record_count=0
        )

    assert [path for path in (tmp_path / "open-meteo").rglob("*") if path.is_file()] == []


def test_hourly_table_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    payload = _hourly_payload(["2015-01-01T00:00", "2015-01-01T01:00"], [0.0, 1.0], [0.0, 1.0])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")

    path = open_meteo_snapshots.write_hourly_table(
        result.frame, "WC-G1", "2015-01-01", "2015-01-02", tmp_path
    )
    assert list(path.parent.glob("*.tmp")) == []
    assert list(path.parent.glob("*")) == [path]


def test_utc_offset_seconds_is_cross_checked_against_the_timezone() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    payload = _hourly_payload(["2015-01-01T00:00"], [0.0], [0.0])
    payload["utc_offset_seconds"] = 0

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        mismatched = client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")

    payload["utc_offset_seconds"] = 28800
    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        matched = client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")

    assert (
        "时区偏移[不一致：响应=0，期望 Asia/Shanghai=28800]" in mismatched.provenance.dataset_notes
    )
    assert (
        "时区偏移[一致：响应=28800，期望 Asia/Shanghai=28800]" in matched.provenance.dataset_notes
    )


def test_precipitation_and_rain_are_kept_as_separate_columns() -> None:
    payload = _hourly_payload(["2015-01-01T00:00"], [1.0], [0.5], latitude=27.79)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with OpenMeteoClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_hourly_rainfall([WC_G1], "2015-01-01", "2015-01-02")

    assert result.frame["precipitation_mm"].tolist() == [1.0]
    assert result.frame["rain_mm"].tolist() == [0.5]

from datetime import date

import httpx
import pytest

from river_sentinel_ml.ingestion.wenzhou_api import (
    SourceAccessError,
    WenzhouWaterLevelClient,
)


def test_iter_rows_paginates_until_reported_total() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["appsecret"] == "test-secret"
        if request.url.path.endswith("/get_total.do"):
            return httpx.Response(200, json={"status": 1, "msg": "ok", "data": 3})
        page_number = int(request.url.params["pageNum"])
        pages = {
            1: [
                {"z_id": "1", "time_d": "2026-01-01 00:00:00", "up_water_level": "3.1"},
                {"z_id": "2", "time_d": "2026-01-01 01:00:00", "up_water_level": "3.2"},
            ],
            2: [
                {"z_id": "3", "time_d": "2026-01-01 02:00:00", "up_water_level": "3.3"}
            ],
        }
        return httpx.Response(200, json={"status": 1, "msg": "ok", "data": pages[page_number]})

    transport = httpx.MockTransport(handler)
    with WenzhouWaterLevelClient("test-secret", transport=transport) as client:
        rows = list(client.iter_rows(page_size=2))

    assert [row["z_id"] for row in rows] == ["1", "2", "3"]


def test_get_update_date_parses_platform_date() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/get_dataupdate_date.do")
        return httpx.Response(200, json={"status": 1, "msg": "ok", "data": "2026-05-12"})

    with WenzhouWaterLevelClient(
        "test-secret", transport=httpx.MockTransport(handler)
    ) as client:
        updated_at = client.get_update_date()

    assert updated_at == date(2026, 5, 12)


def test_page_size_cannot_exceed_platform_limit() -> None:
    with WenzhouWaterLevelClient("test-secret") as client:
        with pytest.raises(ValueError, match="page_size"):
            list(client.iter_rows(page_size=201))


def test_platform_failure_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 0, "msg": "denied", "data": None})

    with WenzhouWaterLevelClient(
        "private-value", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceAccessError, match="denied") as error:
            client.get_total()

    assert "private-value" not in str(error.value)


def test_malformed_total_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 1, "msg": "ok", "data": "three"})

    with WenzhouWaterLevelClient(
        "test-secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceAccessError, match="total"):
            client.get_total()


def test_timeout_is_converted_without_leaking_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("network timeout", request=request)

    with WenzhouWaterLevelClient(
        "private-value", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(SourceAccessError, match="request failed") as error:
            client.get_total()

    assert "private-value" not in str(error.value)

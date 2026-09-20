"""Pagination truncation behaviour of the Wenzhou water-level client.

These cases cover the branch that the frozen contract tests miss: a page that returns
more rows than the platform-declared ``total`` still has to yield at most ``total``
records. All requests run against ``httpx.MockTransport``; the real platform is never
contacted (``API_AUTH_PENDING``).
"""

import httpx
import pytest

from river_sentinel_ml.ingestion.wenzhou_api import WenzhouWaterLevelClient

_PLACEHOLDER_SECRET = "fake-appsecret"


def _row(z_id: str) -> dict[str, object]:
    return {"z_id": z_id, "time_d": "2026-01-01 00:00:00", "up_water_level": "3.1"}


def _transport(
    total: int,
    pages: dict[int, list[dict[str, object]]],
    data_calls: list[int],
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_total.do"):
            return httpx.Response(200, json={"status": 1, "msg": "ok", "data": total})
        page_number = int(request.url.params["pageNum"])
        data_calls.append(page_number)
        page = pages.get(page_number, [])
        return httpx.Response(200, json={"status": 1, "msg": "ok", "data": page})

    return httpx.MockTransport(handler)


def _iter_ids(
    total: int, pages: dict[int, list[dict[str, object]]], page_size: int
) -> tuple[list[object], list[int]]:
    data_calls: list[int] = []
    with WenzhouWaterLevelClient(
        _PLACEHOLDER_SECRET, transport=_transport(total, pages, data_calls)
    ) as client:
        rows = list(client.iter_rows(page_size=page_size))
    return [row["z_id"] for row in rows], data_calls


def test_last_page_is_truncated_to_declared_total() -> None:
    ids, data_calls = _iter_ids(
        total=3,
        pages={1: [_row("1"), _row("2")], 2: [_row("3"), _row("4"), _row("5")]},
        page_size=2,
    )

    assert ids == ["1", "2", "3"]
    assert data_calls == [1, 2]


def test_first_page_never_exceeds_declared_total() -> None:
    ids, _ = _iter_ids(
        total=2,
        pages={1: [_row("1"), _row("2"), _row("3")]},
        page_size=2,
    )

    assert ids == ["1", "2"]


def test_empty_page_stops_without_further_request() -> None:
    ids, data_calls = _iter_ids(
        total=5,
        pages={1: [_row("1"), _row("2")], 2: []},
        page_size=2,
    )

    assert ids == ["1", "2"]
    assert data_calls == [1, 2]


def test_invalid_page_size_is_rejected_before_any_request() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, json={"status": 1, "msg": "ok", "data": 0})

    with WenzhouWaterLevelClient(
        _PLACEHOLDER_SECRET, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError, match="page_size"):
            list(client.iter_rows(page_size=201))

    assert requested == []


def test_transport_errors_stay_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("network timeout", request=request)

    with WenzhouWaterLevelClient(
        _PLACEHOLDER_SECRET, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(RuntimeError) as error:
            list(client.iter_rows(page_size=2))

    assert _PLACEHOLDER_SECRET not in str(error.value)

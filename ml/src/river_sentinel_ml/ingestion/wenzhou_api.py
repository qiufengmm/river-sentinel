"""Read-only client for the Wenzhou open-data water-level interface.

The client targets dataset ``cata_12720`` ("文成县飞云江二期治理工程水位信息"). It only
reads rows in the official field layout; cleaning, deduplication, imputation, unit
conversion and timezone handling are downstream responsibilities.

The ``appsecret`` is sent as a query parameter and is never printed, logged or embedded
in exception messages: httpx error strings may contain the full request URL, so failures
only report the exception type.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any, Self

import httpx

BASE_URL = "https://data.wenzhou.gov.cn/jdop_front/interfaces/cata_12720"
TOTAL_PATH = "/get_total.do"
DATA_PATH = "/get_data.do"
UPDATE_PATH = "/get_dataupdate_date.do"
MAX_PAGE_SIZE = 200
REQUEST_TIMEOUT_SECONDS = 30.0

_UPDATE_DATE_FORMAT = "%Y-%m-%d"


class SourceAccessError(RuntimeError):
    """Raised when the Wenzhou platform cannot be read or returns an unusable payload."""


class WenzhouWaterLevelClient:
    """Thin read-only adapter over the Wenzhou water-level interface."""

    def __init__(self, appsecret: str, transport: httpx.BaseTransport | None = None) -> None:
        self._appsecret = appsecret
        self._client = httpx.Client(
            base_url=BASE_URL,
            transport=transport,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def get_total(self) -> int:
        """Return the record count declared by the platform."""
        payload = self._request_json(TOTAL_PATH)
        if isinstance(payload, bool) or not isinstance(payload, int):
            raise SourceAccessError(
                f"wenzhou water level total must be a non-negative integer, "
                f"got {type(payload).__name__}"
            )
        if payload < 0:
            raise SourceAccessError(
                f"wenzhou water level total must not be negative, got {payload}"
            )
        return payload

    def get_update_date(self) -> date:
        """Return the platform data-update date."""
        payload = self._request_json(UPDATE_PATH)
        if not isinstance(payload, str):
            raise SourceAccessError(
                f"wenzhou water level update date must be a string, got {type(payload).__name__}"
            )
        try:
            return date.fromisoformat(payload.strip())
        except ValueError as exc:
            raise SourceAccessError(
                f"wenzhou water level update date must use the ISO format {_UPDATE_DATE_FORMAT}"
            ) from exc

    def iter_rows(self, page_size: int = MAX_PAGE_SIZE) -> Iterator[dict[str, object]]:
        """Yield raw platform rows page by page, never more than the declared total.

        A page may return more rows than the remaining count implied by ``get_total()``;
        such a page is truncated so the caller never sees more records than the platform
        declared. An empty page stops iteration early.
        """
        if page_size > MAX_PAGE_SIZE or page_size < 1:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}, got {page_size}")

        total = self.get_total()
        produced = 0
        page_number = 1
        while produced < total:
            rows = self._request_rows(page_number=page_number, page_size=page_size)
            if not rows:
                break
            remaining = total - produced
            for row in rows[:remaining]:
                yield row
                produced += 1
            page_number += 1

    def _request_rows(self, page_number: int, page_size: int) -> list[dict[str, object]]:
        payload = self._request_json(DATA_PATH, {"pageNum": page_number, "pageSize": page_size})
        if not isinstance(payload, list):
            raise SourceAccessError(
                f"wenzhou water level data page must be a list of records, "
                f"got {type(payload).__name__}"
            )
        rows: list[dict[str, object]] = []
        for index, row in enumerate(payload):
            if not isinstance(row, dict):
                raise SourceAccessError(
                    f"wenzhou water level row at index {index} must be an object, "
                    f"got {type(row).__name__}"
                )
            rows.append(row)
        return rows

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query: dict[str, Any] = {"appsecret": self._appsecret}
        if params:
            query.update(params)
        try:
            response = self._client.get(path, params=query)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            # Never interpolate the exception text: httpx messages embed the request URL,
            # which carries the appsecret.
            raise SourceAccessError(
                f"wenzhou water level request failed: {type(exc).__name__}"
            ) from exc
        except ValueError as exc:
            raise SourceAccessError(
                "wenzhou water level request failed: response is not valid JSON"
            ) from exc

        if not isinstance(payload, dict):
            raise SourceAccessError(
                f"wenzhou water level response must be a JSON object, got {type(payload).__name__}"
            )
        status = payload.get("status")
        if status != 1:
            message = payload.get("msg")
            raise SourceAccessError(
                f"wenzhou water level platform rejected the request: msg={message!r}"
            )
        return payload.get("data")

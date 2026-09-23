"""Domain errors mapped to the unified response envelope.

每个错误都携带统一错误码、HTTP 状态码和 ``ApiResponse.status``；路由层统一转换，
不在业务服务里拼接 HTTP 响应。
"""

from __future__ import annotations

from typing import Literal

from .schemas.common import (
    AGENT_TOOL_FAILED,
    DATA_UNAVAILABLE,
    INVALID_REQUEST,
    MODEL_UNAVAILABLE,
    ResponseStatus,
    warning_of,
)


class BackendError(Exception):
    """Base error carrying the unified code and HTTP status."""

    code: str = INVALID_REQUEST
    http_status: int = 400
    response_status: ResponseStatus = "error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    @property
    def warning(self) -> str:
        """Warning entry written into the response envelope."""
        return warning_of(self.code, self.message)


class InvalidRequestError(BackendError):
    """Request payload or parameter violates the contract."""

    code = INVALID_REQUEST
    http_status = 400
    response_status: ResponseStatus = "error"


class DataUnavailableError(BackendError):
    """Sample or artifact data cannot be read; nothing may be fabricated."""

    code = DATA_UNAVAILABLE
    http_status = 404
    response_status: Literal["unavailable"] = "unavailable"


class ModelUnavailableError(BackendError):
    """Neither the requested model artifact nor the persistence baseline works."""

    code = MODEL_UNAVAILABLE
    http_status = 503
    response_status: Literal["unavailable"] = "unavailable"


class AgentToolFailedError(BackendError):
    """A whitelisted agent tool failed; the failure reason is preserved."""

    code = AGENT_TOOL_FAILED
    http_status = 502
    response_status: Literal["unavailable"] = "unavailable"

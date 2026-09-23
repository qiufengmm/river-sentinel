"""FastAPI application factory (DS-8A).

``create_app()`` 不依赖外部大模型凭据、实时接口或模型权重：默认使用
``LocalDemoProvider`` 与 ``data/samples/water_level_sample.csv``，缺模型权重时回退
已验证的持久性基线。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.routes import build_router
from .config import Settings
from .errors import BackendError
from .schemas.common import (
    DISCLAIMER,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    ApiResponse,
    Evidence,
    warning_of,
)
from .services.container import build_container

DESCRIPTION = (
    "飞云江水位预测与防汛辅助决策智能体后端（教学科研辅助）。"
    "所有结果都基于历史回放 / 本地快照样例数据，"
    f"{DISCLAIMER}"
)


def _error_response(
    request: Request,
    status_code: int,
    status: str,
    message: str,
    code: str = INVALID_REQUEST,
) -> JSONResponse:
    settings: Settings = request.app.state.settings
    envelope: ApiResponse[Any] = ApiResponse(
        request_id=str(getattr(request.state, "request_id", "unknown")),
        status=status,  # type: ignore[arg-type]
        data=None,
        evidence=[
            Evidence(
                source_url=settings.source_url,
                data_mode=settings.data_mode,
                freshness="unknown",
                tool_trace=["error"],
            )
        ],
        warnings=[warning_of(code, message)],
    )
    response = JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))
    response.headers["X-Request-ID"] = envelope.request_id
    return response


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app with the configured container and error handlers."""
    resolved = settings or Settings()
    container = build_container(resolved)

    app = FastAPI(
        title="River Sentinel 后端与智能体",
        version=resolved.service_version,
        description=DESCRIPTION,
    )
    app.state.settings = resolved
    app.state.container = container

    @app.middleware("http")
    async def _assign_request_id(request: Request, call_next: Any) -> Any:
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", request.state.request_id)
        return response

    @app.exception_handler(BackendError)
    def _backend_error(request: Request, exc: BackendError) -> JSONResponse:
        return _error_response(
            request,
            exc.http_status,
            exc.response_status,
            exc.message,
            code=exc.code,
        )

    @app.exception_handler(RequestValidationError)
    def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            request,
            400,
            "error",
            f"请求参数不合法：{exc.errors()}",
        )

    @app.exception_handler(Exception)
    def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        return _error_response(
            request,
            500,
            "error",
            f"服务内部错误：{type(exc).__name__}；详情见服务端日志",
            code=INTERNAL_ERROR,
        )

    app.include_router(build_router(container), prefix=resolved.api_prefix)
    return app


app = create_app()

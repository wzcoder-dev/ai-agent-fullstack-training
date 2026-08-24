"""统一错误处理：内部错误归一化 + 对外稳定错误码 + 全局异常处理器。

设计依据课程 1-2 normalize_exception（10 类内部错误码 + retryable）
与课程 1-6 GatewayError（稳定对外错误码与 HTTP 状态）。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

logger = logging.getLogger("llm_gateway")

# 内部错误分类（课程 1-2 的 ErrorCode 口径）。
InternalErrorCode = Literal[
    "authentication",
    "invalid_request",
    "rate_limited",
    "overloaded",
    "timeout",
    "network",
    "refusal",
    "truncated",
    "schema_invalid",
    "unknown",
]

# 传输类瞬时故障才允许重试。
RETRYABLE_INTERNAL_CODES = frozenset({"rate_limited", "overloaded", "timeout", "network"})


class UpstreamError(Exception):
    # 上游调用异常的归一化形态：分类码 + 是否可重试。
    def __init__(self, code: InternalErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        self.retryable = code in RETRYABLE_INTERNAL_CODES
        super().__init__(message)


def normalize_upstream_exception(
    exc: Exception,
    retry_statuses: frozenset[int] | set[int] | None = None,
) -> UpstreamError:
    # 把 openai SDK / jsonschema / 未知异常映射为内部错误分类。
    # 注意 APITimeoutError 是 APIConnectionError 的子类，要先判断。
    if isinstance(exc, APITimeoutError):
        return UpstreamError("timeout", "上游请求超时")
    if isinstance(exc, APIConnectionError):
        return UpstreamError("network", "上游网络连接失败")
    if isinstance(exc, APIStatusError) and retry_statuses is not None:
        # 配置了 retry_statuses 时，HTTP 状态码是否可重试完全由配置决定（吸收自课程 1-7）。
        code: InternalErrorCode
        if isinstance(exc, AuthenticationError) or isinstance(exc, PermissionDeniedError):
            code = "authentication"
        elif isinstance(exc, RateLimitError):
            code = "rate_limited"
        elif isinstance(exc, InternalServerError):
            code = "overloaded"
        else:
            code = "invalid_request"
        error = UpstreamError(code, f"上游返回 HTTP {exc.status_code}")
        error.retryable = exc.status_code in retry_statuses
        return error
    if isinstance(exc, AuthenticationError):
        return UpstreamError("authentication", "供应商密钥认证失败")
    if isinstance(exc, PermissionDeniedError):
        return UpstreamError("authentication", "供应商拒绝该密钥访问")
    if isinstance(exc, RateLimitError):
        return UpstreamError("rate_limited", "上游限流")
    if isinstance(exc, InternalServerError):
        return UpstreamError("overloaded", "上游服务内部错误")
    if isinstance(exc, (BadRequestError, NotFoundError, UnprocessableEntityError, ConflictError)):
        return UpstreamError("invalid_request", f"上游拒绝请求: {exc}")
    return UpstreamError("unknown", f"未知上游错误: {exc}")


class GatewayError(Exception):
    # 对外可安全暴露的稳定错误码 + HTTP 状态，错误码表见 DEVELOPMENT.md §6.2。
    def __init__(self, code: str, message: str, status_code: int = 502, request_id: str | None = None) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.request_id = request_id
        super().__init__(message)


def _error_body(code: str, message: Any, request_id: str | None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def register_error_handlers(app: FastAPI) -> None:
    # 统一错误体 {"error": {code, message, request_id}}，所有出口一致。

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        request_id = exc.request_id or getattr(request.state, "request_id", None)
        # 429 附带 Retry-After，客户端可据此退避（吸收自课程 1-7）。
        headers = {"Retry-After": "1"} if exc.status_code == 429 else None
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message, request_id),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=422,
            content=_error_body("invalid_request", "请求体不符合协议", request_id),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # 兜底：记录完整堆栈，对外只暴露稳定码。
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=500,
            content=_error_body("internal_error", "网关内部错误", request_id),
        )

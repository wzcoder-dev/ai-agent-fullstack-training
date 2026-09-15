from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Any, AsyncIterator
from uuid import uuid4

import litellm

from .config import GatewayConfig, RouteTarget
from .models import ChatCompletionRequest
from .repository import GatewayRepository


class GatewayError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class LiteLLMGateway:
    def __init__(self, config: GatewayConfig, repository: GatewayRepository) -> None:
        self.config = config
        self.repository = repository
        self.semaphore = asyncio.Semaphore(config.concurrency)
        self.requests: deque[float] = deque()

    async def complete(self, request: ChatCompletionRequest) -> dict[str, Any]:
        request_id = str(uuid4())
        started = time.monotonic()
        attempts = 0
        last_error: Exception | None = None
        async with self.semaphore:
            self._claim_rate_slot()
            for target in self._targets(request.model):
                for _ in range(target.max_attempts):
                    attempts += 1
                    try:
                        response = await litellm.acompletion(**self._kwargs(request, target, stream=False))
                        payload = response.model_dump()
                        payload["model"] = request.model
                        usage = payload.get("usage") or {}
                        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
                        output_tokens = int(usage.get("completion_tokens", 0) or 0)
                        self.repository.add_call(
                            request_id=request_id, requested_model=request.model, actual_model=target.name,
                            status="success", attempts=attempts, input_tokens=input_tokens,
                            output_tokens=output_tokens, cost_usd=self._cost(target, input_tokens, output_tokens),
                            latency_ms=int((time.monotonic() - started) * 1000),
                            metadata={"stream": False},
                        )
                        return payload
                    except Exception as error:  # LiteLLM normalizes provider exceptions.
                        last_error = error
                        if not self._retryable(error):
                            break
                        await asyncio.sleep(min(0.25 * (2 ** (attempts - 1)), 2.0))
        code = "MODEL_UNAVAILABLE" if last_error else "MODEL_ROUTE_NOT_FOUND"
        self.repository.add_call(
            request_id=request_id, requested_model=request.model, status="failed", attempts=attempts,
            latency_ms=int((time.monotonic() - started) * 1000), error_code=code,
            metadata={"exception_type": type(last_error).__name__ if last_error else None},
        )
        raise GatewayError(code, self._safe_error(last_error), 503)

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[dict[str, Any]]:
        request_id = str(uuid4())
        started = time.monotonic()
        targets = self._targets(request.model)
        last_error: Exception | None = None
        attempts = 0
        async with self.semaphore:
            self._claim_rate_slot()
            for target in targets:
                for _ in range(target.max_attempts):
                    attempts += 1
                    emitted = False
                    input_tokens = 0
                    output_tokens = 0
                    try:
                        response = await litellm.acompletion(**self._kwargs(request, target, stream=True))
                        async for chunk in response:
                            payload = chunk.model_dump()
                            payload["model"] = request.model
                            usage = payload.get("usage") or {}
                            input_tokens = int(usage.get("prompt_tokens", input_tokens) or input_tokens)
                            output_tokens = int(usage.get("completion_tokens", output_tokens) or output_tokens)
                            emitted = True
                            yield payload
                        self.repository.add_call(
                            request_id=request_id, requested_model=request.model, actual_model=target.name,
                            status="success", attempts=attempts, input_tokens=input_tokens,
                            output_tokens=output_tokens, cost_usd=self._cost(target, input_tokens, output_tokens),
                            latency_ms=int((time.monotonic() - started) * 1000), metadata={"stream": True},
                        )
                        return
                    except Exception as error:
                        last_error = error
                        if emitted:
                            raise GatewayError("STREAM_INTERRUPTED", "流式响应中断，不能安全切换模型", 502) from error
                        if not self._retryable(error):
                            break
                        await asyncio.sleep(min(0.25 * (2 ** (attempts - 1)), 2.0))
        self.repository.add_call(
            request_id=request_id, requested_model=request.model, status="failed", attempts=attempts,
            latency_ms=int((time.monotonic() - started) * 1000), error_code="MODEL_UNAVAILABLE",
            metadata={"stream": True, "exception_type": type(last_error).__name__ if last_error else None},
        )
        raise GatewayError("MODEL_UNAVAILABLE", self._safe_error(last_error), 503)

    def _targets(self, alias: str) -> tuple[RouteTarget, ...]:
        targets = self.config.routes.get(alias)
        if not targets:
            raise GatewayError("MODEL_NOT_ALLOWED", f"模型别名不在白名单中: {alias}", 400)
        return targets

    def _kwargs(self, request: ChatCompletionRequest, target: RouteTarget, stream: bool) -> dict[str, Any]:
        api_key = os.getenv(target.api_key_env)
        if not api_key:
            raise GatewayError("MODEL_CREDENTIAL_MISSING", f"缺少环境变量 {target.api_key_env}", 503)
        payload = request.model_dump(exclude_none=True)
        for controlled in ("model", "stream"):
            payload.pop(controlled, None)
        payload.pop("thinking", None)
        if stream:
            payload.setdefault("stream_options", {"include_usage": True})
        return {
            **payload,
            "model": target.litellm_model,
            "api_base": target.api_base,
            "api_key": api_key,
            "stream": stream,
            "timeout": self.config.timeout_seconds,
            "max_retries": 0,
            "extra_body": {"thinking": {"type": "disabled"}},
        }

    def _claim_rate_slot(self) -> None:
        now = time.monotonic()
        while self.requests and now - self.requests[0] >= 60:
            self.requests.popleft()
        if len(self.requests) >= self.config.requests_per_minute:
            raise GatewayError("RATE_LIMITED", "Gateway 每分钟请求数已达上限", 429)
        self.requests.append(now)

    @staticmethod
    def _retryable(error: Exception) -> bool:
        if isinstance(error, GatewayError):
            return error.code not in {"MODEL_CREDENTIAL_MISSING", "MODEL_NOT_ALLOWED"}
        status = getattr(error, "status_code", None)
        return status is None or status in {408, 429, 500, 502, 503, 504}

    @staticmethod
    def _safe_error(error: Exception | None) -> str:
        if isinstance(error, GatewayError):
            return str(error)
        return "模型服务暂时不可用；详细原因仅记录在服务端"

    @staticmethod
    def _cost(target: RouteTarget, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * target.input_per_million + output_tokens * target.output_per_million) / 1_000_000

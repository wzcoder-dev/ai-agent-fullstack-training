"""调用编排：非流式重试/修复循环/fallback 与流式事件生成。

对应课程 1-2 with_retry（指数退避 + jitter + 总 deadline）、
1-5 repairloop（结构化修复循环）、1-6 call_with_fallback / stream_with_fallback
（fallback 链、首块规则、trace 记录）。
客户端断开检测与 cancelled 状态吸收自课程 1-7。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from app.config import GatewayConfig, RetryConfig
from app.core.errors import GatewayError, normalize_upstream_exception
from app.schemas import (
    CallTrace,
    ChatRequest,
    ChatResponse,
    ContentDelta,
    Message,
    PromptSelection,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    SamplingParams,
    Usage,
)
from app.services.prompts import PromptRegistry
from app.services.router import ModelEntry, ModelRouter
from app.services.structured import (
    JsonExtractionError,
    ValidationIssue,
    build_repair_message,
    check_schema_is_valid,
    extract_json,
    validate_against_schema,
)
from app.services.upstream import Provider, StreamChunk
from app.services.usage import UsageStore

logger = logging.getLogger("llm_gateway")

# 流式核心生成器的产出：(seq, 事件)；event 为 None 表示 keep-alive 心跳行。
StreamItem = tuple[int, "BaseModel | None"]

DisconnectCheck = Callable[[], Awaitable[bool]]


def _backoff_delay(attempt: int, retry: RetryConfig) -> float:
    # 指数退避 + 25% 抖动，封顶 max_delay（课程 1-2 口径）。
    delay = min(retry.base_delay * (2 ** (attempt - 1)), retry.max_delay)
    return delay * (1 + random.uniform(0, 0.25))


def _sse_encode(seq: int, payload: dict[str, Any]) -> str:
    return f"id: {seq}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sampling_of(request: ChatRequest) -> SamplingParams:
    return SamplingParams(temperature=request.temperature, top_p=request.top_p, max_tokens=request.max_tokens)


class GatewayService:
    # 网关核心服务：组合 provider 适配、路由熔断、模板、结构化校验与审计。
    def __init__(
        self,
        config: GatewayConfig,
        providers: Mapping[str, Provider],
        router: ModelRouter,
        prompts: PromptRegistry,
        store: UsageStore,
    ) -> None:
        self.config = config
        self.providers = dict(providers)
        self.router = router
        self.prompts = prompts
        self.store = store

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    def prepare(self, request: ChatRequest) -> tuple[list[Message], list[ModelEntry]]:
        # 请求级前置校验：schema 合法性、模板渲染、路由链构建。
        if request.response_schema is not None:
            check_schema_is_valid(request.response_schema)
        messages = self._build_messages(request)
        chain = self.router.build_chain(request.model, require_structured=request.response_schema is not None)
        return messages, chain

    def validate_stream_request(self, request: ChatRequest) -> None:
        # 流式端点的 fail-fast 校验：错误发生在 200 响应头之前，可返回正确状态码。
        if request.response_schema is not None:
            raise GatewayError("unsupported_combination", "流式输出不支持 response_schema", 400)
        self.prepare(request)

    async def chat(self, request: ChatRequest, key_id: str) -> ChatResponse:
        # 非流式：重试 → 结构化提取/校验/修复 → fallback。
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        messages, chain = self.prepare(request)
        retry = self.config.retry
        retry_statuses = frozenset(retry.retry_statuses)
        deadline = started + retry.total_deadline
        attempts = 0
        last_error: Exception | None = None
        saw_transport_failure = False
        attempted_any = False
        sampling = _sampling_of(request)

        for entry in chain:
            if not self.router.breaker.allows(entry.name):
                continue
            attempted_any = True
            provider = self.providers[entry.config.protocol]
            model_messages = list(messages)
            transport_attempts = 0
            repairs_used = 0

            while True:
                transport_attempts += 1
                attempts += 1
                try:
                    result = await provider.complete(
                        entry.config,
                        model_messages,
                        request.timeout_seconds,
                        request.response_schema,
                        sampling,
                    )
                except GatewayError as exc:
                    # 配置类错误（如密钥缺失）：不重试，换下一模型尝试 fallback。
                    last_error = exc
                    break
                except Exception as exc:
                    upstream_error = normalize_upstream_exception(exc, retry_statuses)
                    last_error = upstream_error
                    saw_transport_failure = True
                    if (
                        upstream_error.retryable
                        and transport_attempts < retry.max_attempts_per_model
                        and time.perf_counter() < deadline
                    ):
                        await asyncio.sleep(_backoff_delay(transport_attempts, retry))
                        continue
                    break

                if request.response_schema is None:
                    return await self._finish_success(
                        request_id=request_id,
                        key_id=key_id,
                        requested_model=request.model,
                        entry=entry,
                        prompt=request.prompt,
                        content=result.content,
                        parsed=None,
                        usage=result.usage,
                        started=started,
                        attempts=attempts,
                        ttft_ms=None,
                        finish_reason=result.finish_reason,
                    )

                # 结构化出口：提取 → 校验 → 修复循环（课程 1-5）。
                parsed: Any = None
                issues: list[ValidationIssue]
                try:
                    parsed = extract_json(result.content)
                    issues = validate_against_schema(parsed, request.response_schema)
                except JsonExtractionError:
                    parsed = None
                    issues = [ValidationIssue(path="$", code="invalid_json", message="输出不是合法 JSON")]
                if not issues:
                    return await self._finish_success(
                        request_id=request_id,
                        key_id=key_id,
                        requested_model=request.model,
                        entry=entry,
                        prompt=request.prompt,
                        content=result.content,
                        parsed=parsed,
                        usage=result.usage,
                        started=started,
                        attempts=attempts,
                        ttft_ms=None,
                        finish_reason=result.finish_reason,
                    )
                if repairs_used < self.config.structured.max_repairs:
                    repairs_used += 1
                    assistant_content = result.content if result.content.strip() else "（模型未返回内容）"
                    model_messages = [
                        *model_messages,
                        Message(role="assistant", content=assistant_content),
                        Message(role="user", content=build_repair_message(issues, request.response_schema)),
                    ]
                    continue

                # 修复耗尽：语义失败不 fallback（换模型不保证改善且放大成本，课程 1-6 同策略）。
                error_code = "invalid_json" if parsed is None else "schema_validation_failed"
                latency_ms = int((time.perf_counter() - started) * 1000)
                await self._record_trace(
                    request_id=request_id,
                    key_id=key_id,
                    requested_model=request.model,
                    actual_model=entry.name,
                    prompt=request.prompt,
                    usage=result.usage,
                    latency_ms=latency_ms,
                    ttft_ms=None,
                    attempts=attempts,
                    status="failed",
                    error_code=error_code,
                )
                raise GatewayError(error_code, "模型输出未通过结构化校验（修复重试用尽）", 502, request_id)

            # 该模型重试耗尽或不可重试 → 计熔断，进入下一模型。
            self.router.breaker.record_failure(entry.name)

        latency_ms = int((time.perf_counter() - started) * 1000)
        if not attempted_any:
            await self._record_trace(
                request_id=request_id,
                key_id=key_id,
                requested_model=request.model,
                actual_model=None,
                prompt=request.prompt,
                usage=Usage(input_tokens=0, output_tokens=0),
                latency_ms=latency_ms,
                ttft_ms=None,
                attempts=attempts,
                status="failed",
                error_code="circuit_open",
            )
            raise GatewayError("circuit_open", "路由链上的模型均处于熔断状态", 503, request_id)
        # 全链失败：区分“全部是配置错误”与“存在传输故障”。
        if not saw_transport_failure and isinstance(last_error, GatewayError):
            error_code = "gateway_misconfigured"
            status_code = 503
            message = f"模型凭据未配置: {last_error.message}"
        else:
            error_code = "model_unavailable"
            status_code = 502
            message = "主模型和备用模型均不可用"
        await self._record_trace(
            request_id=request_id,
            key_id=key_id,
            requested_model=request.model,
            actual_model=None,
            prompt=request.prompt,
            usage=Usage(input_tokens=0, output_tokens=0),
            latency_ms=latency_ms,
            ttft_ms=None,
            attempts=attempts,
            status="failed",
            error_code=error_code,
        )
        raise GatewayError(error_code, message, status_code, request_id)

    # ------------------------------------------------------------------
    # 流式：核心事件生成 + SSE 编码两层。
    # 分层是为了让 OpenAI Compatible 入口能复用同一份类型化事件流（吸收自 1-7 的架构）。
    # ------------------------------------------------------------------

    def start_stream_events(
        self,
        request: ChatRequest,
        key_id: str,
        disconnect_check: DisconnectCheck | None = None,
    ) -> tuple[str, AsyncIterator[StreamItem]]:
        # 返回 (request_id, 事件流)；disconnect_check 由 HTTP 层注入（request.is_disconnected）。
        request_id = str(uuid.uuid4())
        return request_id, self._stream_events(request, key_id, request_id, disconnect_check)

    async def stream(
        self,
        request: ChatRequest,
        key_id: str,
        disconnect_check: DisconnectCheck | None = None,
    ) -> AsyncIterator[str]:
        # 自有协议：事件持久化（checkpoint）+ SSE 编码。
        request_id, events = self.start_stream_events(request, key_id, disconnect_check)
        async for seq, event in events:
            if event is None:
                yield ": keep-alive\n\n"
                continue
            payload = event.model_dump(exclude_none=True)
            payload_json = json.dumps(payload, ensure_ascii=False)
            await self.store.append_stream_event(request_id, seq, payload_json)
            yield _sse_encode(seq, payload)

    async def _stream_events(
        self,
        request: ChatRequest,
        key_id: str,
        request_id: str,
        disconnect_check: DisconnectCheck | None,
    ) -> AsyncIterator[StreamItem]:
        # 流式：类型化事件 + 心跳；首块前可切模型，首块后只发流内错误；
        # 客户端断开时停止发送并记录 cancelled（吸收自课程 1-7）。
        started = time.perf_counter()
        messages, chain = self.prepare(request)
        retry_statuses = frozenset(self.config.retry.retry_statuses)
        sampling = _sampling_of(request)
        seq = 0
        attempts = 0

        async def emit(event: BaseModel) -> StreamItem:
            nonlocal seq
            seq += 1
            if isinstance(event, ContentDelta):
                event.seq = seq
            return seq, event

        yield await emit(ResponseStarted(request_id=request_id, model=request.model))

        for entry in chain:
            if not self.router.breaker.allows(entry.name):
                continue
            attempts += 1
            provider = self.providers[entry.config.protocol]
            queue: asyncio.Queue[StreamChunk | Exception | None] = asyncio.Queue()
            pump_task = asyncio.create_task(
                _pump(provider.stream(entry.config, messages, request.timeout_seconds, sampling), queue)
            )
            emitted = False
            ttft_ms: int | None = None
            usage: Usage | None = None
            finish_reason: str | None = None
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=self.config.stream.heartbeat_seconds)
                    except TimeoutError:
                        # 心跳不打断上游读取：生产者在独立 task 中继续。
                        yield 0, None
                        continue
                    if disconnect_check is not None and await disconnect_check():
                        # 客户端已断开：停止发送与上游消费，审计记 cancelled。
                        latency_ms = int((time.perf_counter() - started) * 1000)
                        await self._record_trace(
                            request_id=request_id,
                            key_id=key_id,
                            requested_model=request.model,
                            actual_model=entry.name,
                            prompt=request.prompt,
                            usage=usage or Usage(input_tokens=0, output_tokens=0),
                            latency_ms=latency_ms,
                            ttft_ms=ttft_ms,
                            attempts=attempts,
                            status="cancelled",
                            error_code=None,
                        )
                        return
                    if item is None:
                        break
                    if isinstance(item, BaseException):
                        raise item
                    chunk: StreamChunk = item
                    if chunk.usage is not None:
                        usage = chunk.usage
                    if chunk.finish_reason:
                        finish_reason = chunk.finish_reason
                    if chunk.delta:
                        if not emitted:
                            emitted = True
                            ttft_ms = int((time.perf_counter() - started) * 1000)
                        yield await emit(ContentDelta(seq=0, delta=chunk.delta))
                self.router.breaker.record_success(entry.name)
                latency_ms = int((time.perf_counter() - started) * 1000)
                final_usage = usage or Usage(input_tokens=0, output_tokens=0)
                await self._record_trace(
                    request_id=request_id,
                    key_id=key_id,
                    requested_model=request.model,
                    actual_model=entry.name,
                    prompt=request.prompt,
                    usage=final_usage,
                    latency_ms=latency_ms,
                    ttft_ms=ttft_ms,
                    attempts=attempts,
                    status="success",
                    error_code=None,
                )
                yield await emit(
                    ResponseCompleted(
                        model=entry.name,
                        usage=final_usage,
                        ttft_ms=ttft_ms,
                        latency_ms=latency_ms,
                        attempts=attempts,
                        finish_reason=finish_reason,
                    )
                )
                return
            except Exception as exc:
                if isinstance(exc, GatewayError):
                    error_code, error_message, retryable = exc.code, exc.message, False
                else:
                    upstream_error = normalize_upstream_exception(exc, retry_statuses)
                    error_code, error_message, retryable = (
                        upstream_error.code,
                        upstream_error.message,
                        upstream_error.retryable,
                    )
                self.router.breaker.record_failure(entry.name)
                if not emitted and retryable:
                    continue  # 首块前失败：切下一模型，避免重复文本
                latency_ms = int((time.perf_counter() - started) * 1000)
                await self._record_trace(
                    request_id=request_id,
                    key_id=key_id,
                    requested_model=request.model,
                    actual_model=entry.name if emitted else None,
                    prompt=request.prompt,
                    usage=usage or Usage(input_tokens=0, output_tokens=0),
                    latency_ms=latency_ms,
                    ttft_ms=ttft_ms,
                    attempts=attempts,
                    status="failed",
                    error_code=error_code,
                )
                yield await emit(ResponseFailed(error_code=error_code, message=error_message))
                return
            finally:
                pump_task.cancel()
                with suppress(BaseException):
                    await pump_task

        # 路由链耗尽（全部在首块前失败）。
        latency_ms = int((time.perf_counter() - started) * 1000)
        await self._record_trace(
            request_id=request_id,
            key_id=key_id,
            requested_model=request.model,
            actual_model=None,
            prompt=request.prompt,
            usage=Usage(input_tokens=0, output_tokens=0),
            latency_ms=latency_ms,
            ttft_ms=None,
            attempts=attempts,
            status="failed",
            error_code="model_unavailable",
        )
        yield await emit(ResponseFailed(error_code="model_unavailable", message="主模型和备用模型均不可用"))

    async def replay(self, request_id: str, after_seq: int) -> list[tuple[int, str]]:
        # 从 checkpoint 重放：未知流 404；无新增事件则返回空流。
        rows = await self.store.list_stream_events(request_id, after_seq)
        if not rows and after_seq <= 0:
            raise GatewayError("unknown_stream", "没有该请求的流事件", 404)
        return rows

    def breaker_states(self) -> dict[str, str]:
        states = {name: "closed" for name in self.router.models}
        states.update(self.router.breaker.states())
        return states

    async def aclose(self) -> None:
        for provider in self.providers.values():
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_messages(self, request: ChatRequest) -> list[Message]:
        # 模板渲染的系统消息统一前置注入，Prompt 不分散在各个 Agent 中。
        if request.prompt is None:
            return list(request.messages)
        rendered = self.prompts.render(request.prompt.name, request.prompt.version, request.prompt.variables)
        if not rendered.content.strip():
            raise GatewayError("invalid_request", "模板渲染结果为空", 400)
        return [Message(role="system", content=rendered.content), *request.messages]

    async def _finish_success(
        self,
        *,
        request_id: str,
        key_id: str,
        requested_model: str,
        entry: ModelEntry,
        prompt: PromptSelection | None,
        content: str,
        parsed: Any,
        usage: Usage,
        started: float,
        attempts: int,
        ttft_ms: int | None,
        finish_reason: str | None = None,
    ) -> ChatResponse:
        self.router.breaker.record_success(entry.name)
        latency_ms = int((time.perf_counter() - started) * 1000)
        await self._record_trace(
            request_id=request_id,
            key_id=key_id,
            requested_model=requested_model,
            actual_model=entry.name,
            prompt=prompt,
            usage=usage,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            attempts=attempts,
            status="success",
            error_code=None,
        )
        return ChatResponse(
            request_id=request_id,
            model=entry.name,
            content=content,
            parsed=parsed,
            usage=usage,
            latency_ms=latency_ms,
            attempts=attempts,
            finish_reason=finish_reason,
        )

    async def _record_trace(
        self,
        *,
        request_id: str,
        key_id: str,
        requested_model: str,
        actual_model: str | None,
        prompt: PromptSelection | None,
        usage: Usage,
        latency_ms: int,
        ttft_ms: int | None,
        attempts: int,
        status: str,
        error_code: str | None,
    ) -> None:
        # 留存调用元数据（不含 Prompt 与回答正文），供成本与故障治理。
        trace = CallTrace(
            request_id=request_id,
            timestamp=datetime.now(UTC),
            key_id=key_id,
            requested_model=requested_model,
            actual_model=actual_model,
            prompt_name=prompt.name if prompt else None,
            prompt_version=prompt.version if prompt else None,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            cost_usd=self.router.cost_usd(actual_model, usage.input_tokens, usage.output_tokens, usage.cached_tokens)
            if actual_model
            else 0.0,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            attempts=attempts,
            status=status,  # type: ignore[arg-type]
            error_code=error_code,
        )
        await self.store.record_trace(trace)
        logger.info("llm_call_trace=%s", trace.model_dump_json())


async def _pump(chunks: AsyncIterator[StreamChunk], queue: asyncio.Queue) -> None:
    # 生产者：独立 task 消费上游生成器，异常经队列传回主循环。
    try:
        async for chunk in chunks:
            await queue.put(chunk)
        await queue.put(None)
    except Exception as exc:
        await queue.put(exc)

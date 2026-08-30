"""上游 Provider 适配层：统一接口 + Chat Completions / Responses 两种协议实现。

对应课程 1-2 ModelAdapter（协议归一化、结构化输出分层适配）
与课程 1-6 Provider Protocol（密钥保留在网关进程内，SDK max_retries=0，
重试策略由应用层 gateway.py 统一管理）。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from openai import AsyncOpenAI

from app.config import ModelConfig
from app.core.errors import GatewayError
from app.schemas import Message, SamplingParams, Usage


@dataclass(frozen=True)
class UpstreamResult:
    # 非流式调用结果：原始文本 + token 用量 + 上游 finish_reason。
    content: str
    usage: Usage
    finish_reason: str | None = None


def _cached_tokens(prompt_details: Any) -> int:
    details = getattr(prompt_details, "prompt_tokens_details", None) or getattr(
        prompt_details, "input_tokens_details", None
    )
    return int(getattr(details, "cached_tokens", 0) or 0)


@dataclass(frozen=True)
class StreamChunk:
    # 流式增量：delta 文本；usage/finish_reason 出现在末块。
    delta: str = ""
    usage: Usage | None = None
    finish_reason: str | None = None


class Provider(Protocol):
    # 供应商 Adapter 统一接口，业务编排不依赖具体 SDK。
    async def complete(
        self,
        config: ModelConfig,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
        sampling: SamplingParams | None = None,
        json_mode: bool = False,
    ) -> UpstreamResult: ...

    def stream(
        self,
        config: ModelConfig,
        messages: list[Message],
        timeout_seconds: float,
        sampling: SamplingParams | None = None,
    ) -> AsyncIterator[StreamChunk]: ...


def schema_instruction(schema: dict[str, Any]) -> str:
    # json_object / prompt_only 模式下拼入 system prompt 的约束文本。
    return (
        "只返回一个合法 JSON 对象，必须严格符合下列 JSON Schema，"
        "不要返回 Markdown 或额外文字："
        f"{json.dumps(schema, ensure_ascii=False)}"
    )


JSON_ONLY_INSTRUCTION = "只返回一个合法 JSON 对象，不要返回 Markdown 或额外文字。"


class _BaseOpenAIProvider:
    # 公共客户端管理：按 (base_url, api_key_env) 缓存，密钥缺失即配置错误。
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}

    def _client_for(self, config: ModelConfig) -> AsyncOpenAI:
        cache_key = (config.base_url, config.api_key_env)
        client = self._clients.get(cache_key)
        if client is not None:
            return client
        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise GatewayError(
                "gateway_misconfigured",
                f"Gateway 模型凭据未配置: 环境变量 {config.api_key_env}",
                503,
            )
        kwargs: dict[str, Any] = {"api_key": api_key, "base_url": config.base_url, "max_retries": 0}
        if self._transport is not None:
            # 测试注入：MockTransport 挂在 httpx 客户端上。
            kwargs["http_client"] = httpx.AsyncClient(transport=self._transport, timeout=None)
        client = AsyncOpenAI(**kwargs)
        self._clients[cache_key] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

    @staticmethod
    def _sampling_fields(sampling: SamplingParams | None, *, responses_api: bool) -> dict[str, Any]:
        # 未设置的采样参数不进入请求体；Responses API 的 token 参数名是 max_output_tokens。
        if sampling is None:
            return {}
        fields: dict[str, Any] = {}
        if sampling.temperature is not None:
            fields["temperature"] = sampling.temperature
        if sampling.top_p is not None:
            fields["top_p"] = sampling.top_p
        if sampling.max_tokens is not None:
            fields["max_output_tokens" if responses_api else "max_tokens"] = sampling.max_tokens
        return fields


class OpenAIChatProvider(_BaseOpenAIProvider):
    # OpenAI-compatible Chat Completions 协议（DeepSeek 等绝大多数供应商）。

    async def complete(
        self,
        config: ModelConfig,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
        sampling: SamplingParams | None = None,
        json_mode: bool = False,
    ) -> UpstreamResult:
        # 按模型结构化能力组装请求：原生 schema / json_object / 纯提示三种模式。
        request_messages: list[dict[str, str]] = [message.model_dump() for message in messages]
        request: dict[str, Any] = {
            "model": config.provider_model,
            "messages": request_messages,
            "timeout": timeout_seconds,
            **self._sampling_fields(sampling, responses_api=False),
        }
        if response_schema is not None:
            if config.structured_output_mode == "json_schema":
                request["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "gateway_response",
                        "strict": True,
                        "schema": response_schema,
                    },
                }
            else:  # json_object 与 prompt_only 都退化为提示内嵌 schema
                request["messages"] = [
                    {"role": "system", "content": schema_instruction(response_schema)},
                    *request_messages,
                ]
                if config.structured_output_mode == "json_object":
                    request["response_format"] = {"type": "json_object"}
        elif json_mode and config.structured_output_mode != "none":
            # 客户端 json_object（无 Schema）：能声明 json_object 的供应商直接下发；
            # none 模式模型不设 response_format，由网关出口校验 + 修复兜底。
            request["response_format"] = {"type": "json_object"}
        completion = await self._client_for(config).chat.completions.create(**request)
        choice = completion.choices[0] if completion.choices else None
        content = (choice.message.content or "") if choice else ""
        usage = completion.usage
        return UpstreamResult(
            content=content,
            usage=Usage(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                cached_tokens=_cached_tokens(usage) if usage else 0,
            ),
            finish_reason=choice.finish_reason if choice else None,
        )

    async def stream(
        self,
        config: ModelConfig,
        messages: list[Message],
        timeout_seconds: float,
        sampling: SamplingParams | None = None,
    ) -> AsyncIterator[StreamChunk]:
        # 逐块读取并归一化：末块（include_usage）携带 usage 与 finish_reason。
        stream = await self._client_for(config).chat.completions.create(
            model=config.provider_model,
            messages=[message.model_dump() for message in messages],
            stream=True,
            stream_options={"include_usage": True},
            timeout=timeout_seconds,
            **self._sampling_fields(sampling, responses_api=False),
        )
        async for chunk in stream:
            usage: Usage | None = None
            if chunk.usage:
                usage = Usage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                    cached_tokens=_cached_tokens(chunk.usage),
                )
            choice = chunk.choices[0] if chunk.choices else None
            delta = ""
            finish_reason: str | None = None
            if choice is not None:
                delta = choice.delta.content or ""
                finish_reason = choice.finish_reason
            if delta or usage is not None or finish_reason is not None:
                yield StreamChunk(delta=delta, usage=usage, finish_reason=finish_reason)


class OpenAIResponsesProvider(_BaseOpenAIProvider):
    # OpenAI Responses 协议：input/instructions + 原生 json_schema 结构化输出。

    @staticmethod
    def _split_messages(messages: list[Message]) -> tuple[str | None, list[dict[str, str]]]:
        # Responses 协议的 instructions 承接 system 消息，其余作为 input。
        system_texts = [message.content for message in messages if message.role == "system"]
        instructions = "\n\n".join(system_texts) if system_texts else None
        input_messages = [
            {"role": message.role, "content": message.content} for message in messages if message.role != "system"
        ]
        return instructions, input_messages

    async def complete(
        self,
        config: ModelConfig,
        messages: list[Message],
        timeout_seconds: float,
        response_schema: dict[str, Any] | None,
        sampling: SamplingParams | None = None,
        json_mode: bool = False,
    ) -> UpstreamResult:
        instructions, input_messages = self._split_messages(messages)
        request: dict[str, Any] = {
            "model": config.provider_model,
            "input": input_messages,
            "timeout": timeout_seconds,
            **self._sampling_fields(sampling, responses_api=True),
        }
        if instructions:
            request["instructions"] = instructions
        if response_schema is not None:
            if config.structured_output_mode == "json_schema":
                request["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": "gateway_response",
                        "strict": True,
                        "schema": response_schema,
                    }
                }
            else:  # Responses 协议不支持 json_object，统一退化为提示内嵌
                extra = schema_instruction(response_schema)
                request["instructions"] = f"{instructions}\n\n{extra}" if instructions else extra
        elif json_mode:
            # Responses 协议没有无 Schema 的 json_object 形态，退化为指令约束。
            if instructions:
                request["instructions"] = f"{instructions}\n\n{JSON_ONLY_INSTRUCTION}"
            else:
                request["instructions"] = JSON_ONLY_INSTRUCTION
        response = await self._client_for(config).responses.create(**request)
        usage = response.usage
        return UpstreamResult(
            content=response.output_text or "",
            usage=Usage(
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                cached_tokens=_cached_tokens(usage) if usage else 0,
            ),
            finish_reason=getattr(response, "status", None),
        )

    async def stream(
        self,
        config: ModelConfig,
        messages: list[Message],
        timeout_seconds: float,
        sampling: SamplingParams | None = None,
    ) -> AsyncIterator[StreamChunk]:
        # Responses 流式为类型化事件：output_text.delta 与 response.completed。
        instructions, input_messages = self._split_messages(messages)
        request: dict[str, Any] = {
            "model": config.provider_model,
            "input": input_messages,
            "stream": True,
            "timeout": timeout_seconds,
            **self._sampling_fields(sampling, responses_api=True),
        }
        if instructions:
            request["instructions"] = instructions
        stream = await self._client_for(config).responses.create(**request)
        async for event in stream:
            event_type = getattr(event, "type", "")
            if event_type == "response.output_text.delta":
                yield StreamChunk(delta=getattr(event, "delta", "") or "")
            elif event_type in ("response.completed", "response.incomplete"):
                response = getattr(event, "response", None)
                usage = getattr(response, "usage", None)
                yield StreamChunk(
                    usage=Usage(
                        input_tokens=usage.input_tokens if usage else 0,
                        output_tokens=usage.output_tokens if usage else 0,
                        cached_tokens=_cached_tokens(usage) if usage else 0,
                    ),
                    finish_reason=getattr(response, "status", None),
                )

"""OpenAI Compatible 兼容层（吸收自课程 1-7）。

`POST /v1/chat/completions` 与 `GET /v1/models` 采用 OpenAI 线上协议形态，
客户端可用 OpenAI SDK 零改造接入；内部归一到统一 ChatRequest 走同一编排链路
（重试、fallback、结构化修复、模板、审计全部复用）。

与官方 1-7 的差异是有意为之的治理约束：
- 兼容入口同样遵守"流式与 response_format.json_schema 互斥"（本网关无法在流式
  出口做无损结构化校验，静默放弃校验比明确拒绝更危险）；
- 只接受 system/developer/user/assistant 四种 role，工具调用消息显式 400；
- 未知参数宽容忽略（extra="allow"），但已知参数仍走网关的校验与计费链路。
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core.auth import AuthContext
from app.core.errors import GatewayError
from app.core.ratelimit import chat_rate_limit
from app.schemas import ChatRequest, ContentDelta, Message, PromptSelection, ResponseCompleted, ResponseFailed
from app.services.gateway import GatewayService

router = APIRouter()

_chat = chat_rate_limit()

_ROLE_MAP = {"system": "system", "developer": "system", "user": "user", "assistant": "assistant"}


class CompatMessage(BaseModel):
    # OpenAI 消息形态；content 兼容字符串与文本分段数组。
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None


class CompatChatRequest(BaseModel):
    # OpenAI Chat Completions 请求形态；未知字段宽容忽略。
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[CompatMessage] = Field(min_length=1, max_length=100)
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    response_format: dict[str, Any] | None = None
    stream_options: dict[str, Any] | None = None
    # 网关自有扩展：仍可在兼容入口引用受治理的 Prompt 模板。
    gateway_prompt: PromptSelection | None = None


class CompatModelObject(BaseModel):
    id: str
    object: str = "model"
    created: int
    owned_by: str = "llm-gateway"


class CompatModelList(BaseModel):
    object: str = "list"
    data: list[CompatModelObject]


def _get_gateway(request: Request) -> GatewayService:
    return request.app.state.gateway


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"
        )
    raise GatewayError("unsupported_content", "兼容入口仅支持字符串或文本分段 content", 400)


def _to_internal_messages(payload: CompatChatRequest) -> list[Message]:
    internal: list[Message] = []
    for message in payload.messages:
        mapped = _ROLE_MAP.get(message.role)
        if mapped is None:
            raise GatewayError(
                "unsupported_role",
                f"兼容入口不支持 role={message.role}（网关不转发工具调用协议）",
                400,
            )
        text = _content_to_text(message.content)
        if not text.strip():
            raise GatewayError("invalid_request", "消息 content 不能为空", 400)
        internal.append(Message(role=mapped, content=text))
    return internal


def _schema_from_response_format(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    if response_format is None:
        return None
    format_type = response_format.get("type")
    if format_type == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        if not isinstance(schema, dict):
            raise GatewayError("unsupported_response_format", "response_format.json_schema 缺少 schema", 400)
        return schema
    raise GatewayError(
        "unsupported_response_format",
        "兼容入口仅支持 response_format.type=json_schema",
        400,
    )


def _to_chat_request(payload: CompatChatRequest, *, stream: bool) -> ChatRequest:
    response_schema = _schema_from_response_format(payload.response_format)
    if stream and response_schema is not None:
        raise GatewayError("unsupported_combination", "流式输出不支持 response_format.json_schema", 400)
    return ChatRequest(
        model=payload.model,
        messages=_to_internal_messages(payload),
        stream=stream,
        response_schema=response_schema,
        temperature=payload.temperature,
        top_p=payload.top_p,
        max_tokens=payload.max_completion_tokens or payload.max_tokens,
        prompt=payload.gateway_prompt,
    )


def _openai_usage(usage: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.input_tokens + usage.output_tokens,
    }
    if usage.cached_tokens:
        body["prompt_tokens_details"] = {"cached_tokens": usage.cached_tokens}
    return body


@router.post("/v1/chat/completions")
async def chat_completions(
    payload: CompatChatRequest,
    request: Request,
    auth: AuthContext = Depends(_chat),
    gateway: GatewayService = Depends(_get_gateway),
) -> Any:
    if payload.stream:
        # 兼容流式：复用类型化事件流，翻译为 OpenAI chunk 形态。
        chat_request = _to_chat_request(payload, stream=True)
        gateway.validate_stream_request(chat_request)
        request_id, events = gateway.start_stream_events(
            chat_request, auth.key_id, disconnect_check=request.is_disconnected
        )
        include_usage = bool((payload.stream_options or {}).get("include_usage"))
        requested_model = payload.model

        async def generate():
            async for _, event in events:
                if event is None:
                    yield ": keep-alive\n\n"
                elif isinstance(event, ContentDelta):
                    yield _chunk(request_id, requested_model, delta={"content": event.delta})
                elif isinstance(event, ResponseCompleted):
                    usage = _openai_usage(event.usage) if include_usage else None
                    yield _chunk(
                        request_id,
                        requested_model,
                        finish_reason=event.finish_reason or "stop",
                        usage=usage,
                    )
                    yield "data: [DONE]\n\n"
                elif isinstance(event, ResponseFailed):
                    error = json.dumps(
                        {"error": {"message": event.message, "type": "gateway_error", "code": event.error_code}},
                        ensure_ascii=False,
                    )
                    yield f"data: {error}\n\n"
                    yield "data: [DONE]\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "X-Request-ID": request_id,
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    chat_request = _to_chat_request(payload, stream=False)
    result = await gateway.chat(chat_request, auth.key_id)
    completion = {
        "id": f"chatcmpl-{result.request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.content},
                "finish_reason": result.finish_reason or "stop",
            }
        ],
        "usage": _openai_usage(result.usage),
    }
    return JSONResponse(completion, headers={"X-Request-ID": result.request_id})


def _chunk(
    request_id: str,
    model: str,
    *,
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/v1/models", response_model=CompatModelList)
async def models(
    _: AuthContext = Depends(_chat),
    gateway: GatewayService = Depends(_get_gateway),
) -> CompatModelList:
    # OpenAI 形态的模型列表（只暴露白名单别名）；详细能力信息在 /admin/models。
    now = int(time.time())
    return CompatModelList(data=[CompatModelObject(id=name, created=now) for name in sorted(gateway.router.models)])

"""HTTP 端点：chat / responses / 流式与重放 / 模板与管理接口 / 健康检查。

课程 1-6 的三个端点（/v1/llm、/v1/llm/stream、/v1/traces）扩展为完整 API 面，
鉴权与限流经依赖注入（app.core）。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.core.auth import AuthContext, require_scopes
from app.core.errors import GatewayError
from app.core.ratelimit import chat_rate_limit
from app.schemas import (
    ChatRequest,
    ChatResponse,
    Message,
    ModelInfo,
    PromptTemplateInfo,
    RenderPreviewRequest,
    RenderPreviewResponse,
    ResponsesRequest,
    UsageSummaryRow,
)
from app.services.gateway import GatewayService

router = APIRouter()

_chat = chat_rate_limit()
_admin = require_scopes("admin")


def get_gateway(request: Request) -> GatewayService:
    return request.app.state.gateway


@router.post("/v1/chat", response_model=ChatResponse)
async def create_chat(
    request: ChatRequest,
    auth: AuthContext = Depends(_chat),
    gateway: GatewayService = Depends(get_gateway),
) -> ChatResponse:
    # FastAPI 在入口校验请求、response_model 校验统一响应出口。
    if request.stream:
        raise GatewayError("use_stream_endpoint", "流式请求请使用 /v1/chat/stream", 400)
    return await gateway.chat(request, auth.key_id)


@router.post("/v1/chat/stream")
async def create_chat_stream(
    body: ChatRequest,
    request: Request,
    auth: AuthContext = Depends(_chat),
    gateway: GatewayService = Depends(get_gateway),
) -> StreamingResponse:
    # fail-fast：模板/模型等错误在响应头之前抛出，保持正确的 HTTP 状态码。
    gateway.validate_stream_request(body)
    # 断开检测：客户端断开后立即停止上游消费并记 cancelled（吸收自课程 1-7）。
    return StreamingResponse(
        gateway.stream(body, auth.key_id, disconnect_check=request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/v1/streams/{request_id}/events")
async def replay_stream(
    request_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    auth: AuthContext = Depends(_chat),
    gateway: GatewayService = Depends(get_gateway),
) -> StreamingResponse:
    # 从 SQLite checkpoint 重放已持久化事件，断线客户端按 Last-Event-ID 续读。
    try:
        after_seq = int(last_event_id) if last_event_id else 0
    except ValueError:
        raise GatewayError("invalid_request", "Last-Event-ID 必须是整数", 400) from None
    # 在响应开始前完成查询：未知流以 404 返回而不是破坏已开始的流。
    events = await gateway.replay(request_id, after_seq)

    async def event_source() -> Any:
        for seq, payload in events:
            yield f"id: {seq}\ndata: {payload}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.post("/v1/responses", response_model=ChatResponse)
async def create_response(
    body: ResponsesRequest,
    auth: AuthContext = Depends(_chat),
    gateway: GatewayService = Depends(get_gateway),
) -> ChatResponse:
    # Responses 风格薄入口：归一为统一协议后走同一编排链路。
    if body.instructions is not None and body.prompt is not None:
        raise GatewayError("unsupported_combination", "instructions 与 prompt 模板不能同时使用", 400)
    messages: list[Message] = []
    if body.instructions is not None:
        messages.append(Message(role="system", content=body.instructions))
    messages.append(Message(role="user", content=body.input))
    chat_request = ChatRequest(
        model=body.model,
        messages=messages,
        response_schema=body.response_schema,
        timeout_seconds=body.timeout_seconds,
        prompt=body.prompt,
    )
    return await gateway.chat(chat_request, auth.key_id)


@router.get("/admin/models", response_model=list[ModelInfo])
async def list_models(
    _: AuthContext = Depends(_admin),
    gateway: GatewayService = Depends(get_gateway),
) -> list[ModelInfo]:
    # 详细能力与定价信息（OpenAI 形态的公开列表在 /v1/models，见 app/api/compat.py）。
    return gateway.router.model_infos()


def _template_info(template: Any) -> PromptTemplateInfo:
    return PromptTemplateInfo(
        name=template.name,
        version=template.version,
        description=template.description,
        variables=list(template.variables),
        template_sha256=template.template_sha256,
    )


@router.get("/v1/prompts", response_model=list[PromptTemplateInfo])
async def list_prompts(
    _: AuthContext = Depends(_chat),
    gateway: GatewayService = Depends(get_gateway),
) -> list[PromptTemplateInfo]:
    return [_template_info(template) for template in gateway.prompts.list()]


@router.get("/v1/prompts/{name}/{version}", response_model=PromptTemplateInfo)
async def get_prompt(
    name: str,
    version: str,
    _: AuthContext = Depends(_chat),
    gateway: GatewayService = Depends(get_gateway),
) -> PromptTemplateInfo:
    template = gateway.prompts.get(name, version)
    if template is None:
        raise GatewayError("unknown_prompt_template", f"Prompt 模板不存在: {name} {version}", 400)
    return _template_info(template)


@router.post("/v1/prompts/{name}/{version}/render", response_model=RenderPreviewResponse)
async def render_prompt(
    name: str,
    version: str,
    body: RenderPreviewRequest,
    _: AuthContext = Depends(_admin),
    gateway: GatewayService = Depends(get_gateway),
) -> RenderPreviewResponse:
    rendered = gateway.prompts.render(name, version, body.variables)
    return RenderPreviewResponse(system_message=rendered.content, sha256=rendered.sha256)


@router.get("/v1/traces")
async def list_traces(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: AuthContext = Depends(_admin),
    gateway: GatewayService = Depends(get_gateway),
) -> dict[str, Any]:
    traces, total = await gateway.store.list_traces(limit=limit, offset=offset)
    return {"items": traces, "total": total}


@router.get("/v1/usage", response_model=list[UsageSummaryRow])
async def usage_summary(
    group_by: Literal["model", "key"] = Query(default="model"),
    _: AuthContext = Depends(_admin),
    gateway: GatewayService = Depends(get_gateway),
) -> list[UsageSummaryRow]:
    return await gateway.store.usage_summary(group_by)


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    gateway: GatewayService = request.app.state.gateway
    return {"status": "ok", "models": gateway.breaker_states()}


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> dict[str, str]:
    # 存活≠就绪：依赖（配置、模板、数据库）在 create_app 期已组装完成才应接流量。
    return {"status": "ready", "service": "llm-gateway"}

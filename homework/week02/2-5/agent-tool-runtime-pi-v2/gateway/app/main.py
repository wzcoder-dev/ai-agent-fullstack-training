from __future__ import annotations

import json
import os
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from .config import load_config
from .models import ChatCompletionRequest, OrderStatusInput, OrderStatusOutput, PromptRenderRequest
from .prompts import PromptRegistry
from .repository import GatewayRepository
from .service import GatewayError, LiteLLMGateway

config = load_config()
repository = GatewayRepository(config.database)
prompts = PromptRegistry(config.prompts_file)
gateway = LiteLLMGateway(config, repository)
app = FastAPI(title="Stage One LiteLLM Gateway", version="2.0.0")


def require_api_key(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = os.getenv(config.api_key_env)
    if not expected:
        raise HTTPException(503, {"code": "GATEWAY_CREDENTIAL_MISSING"})
    if authorization != f"Bearer {expected}":
        raise HTTPException(401, {"code": "UNAUTHORIZED"})


@app.exception_handler(GatewayError)
async def gateway_error_handler(_request, error: GatewayError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        content={"error": {"code": error.code, "message": str(error), "type": "gateway_error"}},
        status_code=error.status_code,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models", dependencies=[Depends(require_api_key)])
async def models() -> dict:
    return {"object": "list", "data": [{"id": alias, "object": "model", "owned_by": "phase-gateway"} for alias in config.routes]}


@app.post("/v1/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(request: ChatCompletionRequest):
    if not request.stream:
        return await gateway.complete(request)

    async def encode() -> AsyncIterator[str]:
        async for chunk in gateway.stream(request):
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(encode(), media_type="text/event-stream")


@app.post("/v1/prompts/{name}/render", dependencies=[Depends(require_api_key)])
async def render_prompt(name: str, request: PromptRenderRequest):
    try:
        return prompts.render(name, request.version, request.variables)
    except KeyError as error:
        raise HTTPException(404, {"code": "PROMPT_NOT_FOUND", "message": str(error)}) from error
    except ValueError as error:
        raise HTTPException(422, {"code": "PROMPT_RENDER_FAILED", "message": str(error)}) from error


@app.post("/internal/tools/order.get_status", response_model=OrderStatusOutput, dependencies=[Depends(require_api_key)])
async def order_status(
    request: OrderStatusInput,
    x_tenant_id: Annotated[str, Header(alias="X-Tenant-Id")],
    _x_user_id: Annotated[str, Header(alias="X-User-Id")],
    _x_trace_id: Annotated[str | None, Header(alias="X-Trace-Id")] = None,
) -> OrderStatusOutput:
    expected_prefix = {"tenant_a": "ORD-A-", "tenant_b": "ORD-B-"}.get(x_tenant_id)
    if not expected_prefix or not request.order_id.startswith(expected_prefix):
        raise HTTPException(403, {"code": "RESOURCE_SCOPE_DENIED"})
    status = "exception" if request.order_id.endswith("9001") else "shipped"
    return OrderStatusOutput(
        order_id=request.order_id,
        status=status,
        source="order-service",
        customer_email="customer@example.com",
    )


@app.get("/internal/usage", dependencies=[Depends(require_api_key)])
async def usage(limit: Annotated[int, Query(ge=1, le=500)] = 100):
    return repository.list_calls(limit)

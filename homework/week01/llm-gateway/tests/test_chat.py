"""非流式 Chat 基础链路与入口约束。"""

from __future__ import annotations

import httpx

from tests.conftest import chat_request_bodies, simple_chat_request


async def test_chat_success(client: httpx.AsyncClient, harness) -> None:
    response = await client.post("/v1/chat", json=simple_chat_request())
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "general-primary"
    assert body["content"] == "hello from primary.test"
    assert body["usage"] == {"input_tokens": 5, "output_tokens": 3, "cached_tokens": 0}
    assert body["attempts"] == 1
    assert body["request_id"]
    assert response.headers["x-request-id"]


async def test_upstream_receives_exact_messages(client: httpx.AsyncClient, harness) -> None:
    await client.post(
        "/v1/chat",
        json={"model": "general-primary", "messages": [{"role": "user", "content": "解释网关"}]},
    )
    bodies = chat_request_bodies(harness.chat_transport)
    assert len(bodies) == 1
    assert bodies[0]["model"] == "primary-model"
    assert bodies[0]["messages"] == [{"role": "user", "content": "解释网关"}]


async def test_missing_bearer_token_rejected(harness) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://t") as c:
        response = await c.post("/v1/chat", json=simple_chat_request())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_invalid_bearer_token_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat", json=simple_chat_request(), headers={"authorization": "Bearer wrong-key-000000"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_unknown_field_rejected_with_422(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat", json=simple_chat_request(bogus_field="x"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_unknown_model_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat", json=simple_chat_request(model="no-such-model"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_model"


async def test_stream_flag_must_use_stream_endpoint(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat", json=simple_chat_request(stream=True))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "use_stream_endpoint"


async def test_structured_output_unsupported_model(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat",
        json=simple_chat_request(model="plain-model", response_schema={"type": "object"}),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "structured_output_unsupported"


async def test_healthz_without_auth(harness) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://t") as c:
        response = await c.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["models"]["general-primary"] == "closed"


async def test_admin_models_endpoint_scope(client: httpx.AsyncClient, admin_client: httpx.AsyncClient) -> None:
    denied = await client.get("/admin/models")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "insufficient_scope"

    allowed = await admin_client.get("/admin/models")
    assert allowed.status_code == 200
    names = {model["name"] for model in allowed.json()}
    assert names == {"general-primary", "general-backup", "openai-responses", "plain-model", "fast-pool"}
    primary = next(m for m in allowed.json() if m["name"] == "general-primary")
    assert primary["fallbacks"] == ["general-backup"]
    assert primary["structured_output_mode"] == "json_object"


async def test_readyz(harness) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://t") as c:
        response = await c.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"

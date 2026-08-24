"""结构化输出：提取、修复循环、失败码与请求 schema 校验。"""

from __future__ import annotations

import httpx

from tests.conftest import ANSWER_SCHEMA, chat_completion, chat_request_bodies, simple_chat_request


async def test_valid_json_parsed(client: httpx.AsyncClient, harness) -> None:
    harness.chat_transport.scripts["primary.test"] = [chat_completion('{"answer": "42"}')]
    response = await client.post(
        "/v1/chat", json=simple_chat_request(response_schema=ANSWER_SCHEMA)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["parsed"] == {"answer": "42"}
    assert body["attempts"] == 1


async def test_schema_injected_for_json_object_mode(client: httpx.AsyncClient, harness) -> None:
    # json_object 模式应把 schema 拼进首条 system 消息并设置 response_format。
    harness.chat_transport.scripts["primary.test"] = [chat_completion('{"answer": "x"}')]
    await client.post("/v1/chat", json=simple_chat_request(response_schema=ANSWER_SCHEMA))
    request_body = chat_request_bodies(harness.chat_transport)[0]
    assert request_body["response_format"] == {"type": "json_object"}
    first_message = request_body["messages"][0]
    assert first_message["role"] == "system"
    assert "JSON Schema" in first_message["content"]


async def test_markdown_fence_extracted(client: httpx.AsyncClient, harness) -> None:
    harness.chat_transport.scripts["primary.test"] = [
        chat_completion('结果是：\n```json\n{"answer": "围栏内"}\n```')
    ]
    response = await client.post(
        "/v1/chat", json=simple_chat_request(response_schema=ANSWER_SCHEMA)
    )
    assert response.status_code == 200
    assert response.json()["parsed"] == {"answer": "围栏内"}


async def test_repair_loop_recovers_schema_violation(client: httpx.AsyncClient, harness) -> None:
    # 首次类型错误（answer 应为 string），修复循环后成功。
    harness.chat_transport.scripts["primary.test"] = [
        chat_completion('{"answer": 123}'),
        chat_completion('{"answer": "修好了"}'),
    ]
    response = await client.post(
        "/v1/chat", json=simple_chat_request(response_schema=ANSWER_SCHEMA)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["parsed"] == {"answer": "修好了"}
    assert body["attempts"] == 2
    # 修复请求应包含原输出（assistant）与修复提示（user）。
    repair_body = chat_request_bodies(harness.chat_transport)[1]
    roles = [message["role"] for message in repair_body["messages"]]
    assert roles[-2:] == ["assistant", "user"]
    assert "保持原任务含义不变" in repair_body["messages"][-1]["content"]


async def test_repair_loop_recovers_invalid_json(client: httpx.AsyncClient, harness) -> None:
    harness.chat_transport.scripts["primary.test"] = [
        chat_completion("抱歉，这不是 JSON"),
        chat_completion('{"answer": "现在合法了"}'),
    ]
    response = await client.post(
        "/v1/chat", json=simple_chat_request(response_schema=ANSWER_SCHEMA)
    )
    assert response.status_code == 200
    assert response.json()["parsed"] == {"answer": "现在合法了"}


async def test_repair_exhaustion_fails_without_fallback(
    client: httpx.AsyncClient, harness, admin_client: httpx.AsyncClient
) -> None:
    # 1 次初始 + max_repairs=2 次修复后仍失败 → 502，且不切备用模型。
    harness.chat_transport.scripts["primary.test"] = [
        chat_completion('{"answer": 1}') for _ in range(3)
    ]
    response = await client.post(
        "/v1/chat", json=simple_chat_request(response_schema=ANSWER_SCHEMA)
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "schema_validation_failed"
    # 全部请求都打在主模型上（没有 fallback）。
    hosts = {request.url.host for request in harness.chat_transport.requests}
    assert hosts == {"primary.test"}

    traces = (await admin_client.get("/v1/traces")).json()
    failed = [item for item in traces["items"] if item["status"] == "failed"]
    assert failed and failed[0]["error_code"] == "schema_validation_failed"
    assert failed[0]["attempts"] == 3


async def test_request_schema_validity_checked(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat", json=simple_chat_request(response_schema={"type": "banana"})
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json_schema"

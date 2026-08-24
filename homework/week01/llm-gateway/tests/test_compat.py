"""OpenAI Compatible 兼容层（吸收自课程 1-7）。"""

from __future__ import annotations

import json

import httpx

from tests.conftest import BACKUP_HOST, PRIMARY_HOST, chat_completion, chat_request_bodies


def _compat_payload(**overrides) -> dict:
    payload = {"model": "general-primary", "messages": [{"role": "user", "content": "你好"}]}
    payload.update(overrides)
    return payload


def _parse_chunks(text: str) -> list[dict]:
    lines = [line for line in text.split("\n") if line.startswith("data: ") and line != "data: [DONE]"]
    return [json.loads(line[6:]) for line in lines]


async def test_compat_non_stream_response_shape(client: httpx.AsyncClient, harness) -> None:
    response = await client.post("/v1/chat/completions", json=_compat_payload(temperature=0.3, max_tokens=128))
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "general-primary"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "hello from primary.test"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 3,
        "total_tokens": 8,
    }
    assert response.headers["x-request-id"]

    # 采样参数应透传到上游请求体。
    upstream = chat_request_bodies(harness.chat_transport)[-1]
    assert upstream["temperature"] == 0.3
    assert upstream["max_tokens"] == 128


async def test_compat_maps_roles_and_content_parts(client: httpx.AsyncClient, harness) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=_compat_payload(
            messages=[
                {"role": "developer", "content": "你是简洁助手"},
                {"role": "user", "content": [{"type": "text", "text": "多段"}, {"type": "text", "text": "内容"}]},
            ]
        ),
    )
    assert response.status_code == 200
    upstream = chat_request_bodies(harness.chat_transport)[-1]
    assert upstream["messages"][0] == {"role": "system", "content": "你是简洁助手"}
    assert upstream["messages"][1] == {"role": "user", "content": "多段内容"}


async def test_compat_rejects_tool_role(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=_compat_payload(messages=[{"role": "user", "content": "hi"}, {"role": "tool", "content": "x"}]),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_role"


async def test_compat_structured_output_via_response_format(client: httpx.AsyncClient, harness) -> None:
    harness.chat_transport.scripts[PRIMARY_HOST] = [chat_completion('{"answer": "42"}')]
    response = await client.post(
        "/v1/chat/completions",
        json=_compat_payload(
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                },
            }
        ),
    )
    assert response.status_code == 200
    assert json.loads(response.json()["choices"][0]["message"]["content"]) == {"answer": "42"}

    # 兼容入口走同一结构化链路：json_object 模式下 schema 已注入 system 消息。
    upstream = chat_request_bodies(harness.chat_transport)[0]
    assert upstream["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in upstream["messages"][0]["content"]


async def test_compat_rejects_other_response_format_types(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=_compat_payload(response_format={"type": "json_object"}),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_response_format"


async def test_compat_stream_chunks_and_done(client: httpx.AsyncClient) -> None:
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json=_compat_payload(stream=True, stream_options={"include_usage": True}),
    ) as response:
        assert response.status_code == 200
        assert response.headers["x-request-id"]
        text = (await response.aread()).decode("utf-8")

    assert text.endswith("data: [DONE]\n\n")
    chunks = _parse_chunks(text)
    deltas = [c for c in chunks if c["choices"][0]["delta"].get("content")]
    assert [c["choices"][0]["delta"]["content"] for c in deltas] == ["默认", "流式", "回复"]
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"] == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)


async def test_compat_stream_without_usage_option(client: httpx.AsyncClient) -> None:
    async with client.stream("POST", "/v1/chat/completions", json=_compat_payload(stream=True)) as response:
        text = (await response.aread()).decode("utf-8")
    chunks = _parse_chunks(text)
    assert all("usage" not in chunk for chunk in chunks)


async def test_compat_stream_error_translated(client: httpx.AsyncClient, harness) -> None:
    # 主备全部连接失败 → 兼容层翻译为 OpenAI 风格 SSE 错误 + [DONE]。
    harness.chat_transport.scripts[PRIMARY_HOST] = [httpx.ConnectError("down")]
    harness.chat_transport.scripts[BACKUP_HOST] = [httpx.ConnectError("down")]
    async with client.stream("POST", "/v1/chat/completions", json=_compat_payload(stream=True)) as response:
        text = (await response.aread()).decode("utf-8")
    error_lines = [
        json.loads(line[6:])
        for line in text.split("\n")
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert "error" in error_lines[-1]
    assert error_lines[-1]["error"]["code"] == "model_unavailable"
    assert text.endswith("data: [DONE]\n\n")


async def test_compat_stream_rejects_json_schema(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json=_compat_payload(
            stream=True,
            response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": {"type": "object"}}},
        ),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_combination"


async def test_compat_models_list_openai_shape(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    ids = {item["id"] for item in body["data"]}
    assert "general-primary" in ids
    assert all(item["object"] == "model" for item in body["data"])


async def test_compat_ignores_unknown_fields(client: httpx.AsyncClient) -> None:
    # OpenAI SDK 升级带来的未知参数不应让兼容入口 422。
    response = await client.post(
        "/v1/chat/completions",
        json=_compat_payload(frequency_penalty=0.5, some_future_param="x"),
    )
    assert response.status_code == 200

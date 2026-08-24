"""Responses 风格入口与 Responses 上游协议。"""

from __future__ import annotations

import json

import httpx

from tests.conftest import ANSWER_SCHEMA, OPENAI_HOST, responses_completion


def _responses_bodies(harness) -> list[dict]:
    return [
        json.loads(request.content)
        for request in harness.responses_transport.requests
        if request.url.path.endswith("/responses")
    ]


async def test_responses_endpoint_success(client: httpx.AsyncClient, harness) -> None:
    response = await client.post(
        "/v1/responses", json={"model": "openai-responses", "input": "你好"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "openai-responses"
    assert body["content"] == "hello from responses"

    upstream = _responses_bodies(harness)[-1]
    assert upstream["model"] == "responses-model"
    assert upstream["input"] == [{"role": "user", "content": "你好"}]


async def test_responses_structured_uses_native_json_schema(client: httpx.AsyncClient, harness) -> None:
    harness.responses_transport.scripts[OPENAI_HOST] = [responses_completion('{"answer": "42"}')]
    response = await client.post(
        "/v1/responses",
        json={"model": "openai-responses", "input": "给答案", "response_schema": ANSWER_SCHEMA},
    )
    assert response.status_code == 200
    assert response.json()["parsed"] == {"answer": "42"}

    upstream = _responses_bodies(harness)[-1]
    text_format = upstream["text"]["format"]
    assert text_format["type"] == "json_schema"
    assert text_format["strict"] is True
    assert text_format["schema"] == ANSWER_SCHEMA


async def test_responses_instructions_become_instructions_param(client: httpx.AsyncClient, harness) -> None:
    await client.post(
        "/v1/responses",
        json={"model": "openai-responses", "input": "你好", "instructions": "你是简洁助手"},
    )
    upstream = _responses_bodies(harness)[-1]
    assert upstream["instructions"] == "你是简洁助手"


async def test_instructions_and_prompt_mutually_exclusive(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/responses",
        json={
            "model": "openai-responses",
            "input": "你好",
            "instructions": "你是简洁助手",
            "prompt": {"name": "qa_assistant", "version": "v1", "variables": {"persona": "客服"}},
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_combination"


async def test_stream_via_responses_protocol(client: httpx.AsyncClient) -> None:
    async with client.stream(
        "POST",
        "/v1/chat/stream",
        json={"model": "openai-responses", "messages": [{"role": "user", "content": "讲个故事"}]},
    ) as response:
        assert response.status_code == 200
        text = (await response.aread()).decode("utf-8")

    data_lines = [
        json.loads(line[6:]) for line in text.split("\n") if line.startswith("data: ")
    ]
    deltas = [event["delta"] for event in data_lines if event["type"] == "content.delta"]
    assert deltas == ["默认", "回复"]
    completed = data_lines[-1]
    assert completed["type"] == "response.completed"
    assert completed["model"] == "openai-responses"
    assert completed["usage"]["input_tokens"] == 5

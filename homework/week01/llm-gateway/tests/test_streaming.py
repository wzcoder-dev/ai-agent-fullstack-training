"""流式协议：事件序列、互斥约束、首块前 fallback、心跳与 checkpoint 重放。"""

from __future__ import annotations

import httpx

from tests.conftest import (
    ANSWER_SCHEMA,
    PRIMARY_HOST,
    build_harness,
    parse_sse,
    simple_chat_request,
    slow_stream_sse,
)


async def _stream_text(client: httpx.AsyncClient, payload: dict) -> tuple[int, str]:
    async with client.stream("POST", "/v1/chat/stream", json=payload) as response:
        text = (await response.aread()).decode("utf-8")
        return response.status_code, text


async def test_stream_event_sequence(client: httpx.AsyncClient) -> None:
    status, text = await _stream_text(client, simple_chat_request())
    assert status == 200
    events = parse_sse(text)
    seqs = [seq for seq, _ in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    started = events[0][1]
    assert started["type"] == "response.started"
    request_id = started["request_id"]

    deltas = [event for _, event in events if event["type"] == "content.delta"]
    assert [d["delta"] for d in deltas] == ["默认", "流式", "回复"]
    assert all(d["seq"] > 0 for d in deltas)

    completed = events[-1][1]
    assert completed["type"] == "response.completed"
    assert completed["model"] == "general-primary"
    assert completed["usage"] == {"input_tokens": 5, "output_tokens": 3, "cached_tokens": 0}
    assert completed["ttft_ms"] is not None
    assert completed["attempts"] == 1
    assert completed["finish_reason"] == "stop"
    assert request_id


async def test_stream_rejects_response_schema(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/stream", json=simple_chat_request(response_schema=ANSWER_SCHEMA)
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_combination"


async def test_stream_unknown_model_rejected_before_200(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat/stream", json=simple_chat_request(model="nope"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_model"


async def test_fallback_before_first_chunk(client: httpx.AsyncClient, harness) -> None:
    # 主模型连接失败发生在首块之前 → 切备用模型，不产生重复文本。
    harness.chat_transport.scripts[PRIMARY_HOST] = [httpx.ConnectError("primary down")]
    status, text = await _stream_text(client, simple_chat_request())
    assert status == 200
    events = parse_sse(text)
    completed = events[-1][1]
    assert completed["type"] == "response.completed"
    assert completed["model"] == "general-backup"
    assert completed["attempts"] == 2
    # 事件序号保持连续：主模型没有发过任何业务事件。
    seqs = [seq for seq, _ in events]
    assert seqs == list(range(1, len(events) + 1))


async def test_streaming_requires_auth(harness) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://t") as c:
        response = await c.post("/v1/chat/stream", json=simple_chat_request())
    assert response.status_code == 401


async def test_checkpoint_replay_with_last_event_id(client: httpx.AsyncClient) -> None:
    _, text = await _stream_text(client, simple_chat_request())
    events = parse_sse(text)
    request_id = events[0][1]["request_id"]

    replay = await client.get(f"/v1/streams/{request_id}/events")
    assert replay.status_code == 200
    assert parse_sse(replay.text) == events

    partial = await client.get(
        f"/v1/streams/{request_id}/events", headers={"Last-Event-ID": "2"}
    )
    assert partial.status_code == 200
    assert parse_sse(partial.text) == events[2:]

    unknown = await client.get("/v1/streams/does-not-exist/events")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "unknown_stream"


async def test_heartbeat_emitted_for_slow_upstream(tmp_path, monkeypatch) -> None:
    harness = build_harness(tmp_path, monkeypatch, heartbeat_seconds=0.05)
    harness.chat_transport.scripts[PRIMARY_HOST] = [slow_stream_sse(["慢"], delay=0.3)]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness.app),
            base_url="http://t",
            headers={"authorization": "Bearer gw-chat-test-key-0001"},
            timeout=10,
        ) as client:
            _, text = await _stream_text(client, simple_chat_request())
    finally:
        await harness.app.state.gateway.aclose()
        harness.app.state.store.close()
    assert ": keep-alive" in text
    deltas = [event for _, event in parse_sse(text) if event["type"] == "content.delta"]
    assert [d["delta"] for d in deltas] == ["慢"]

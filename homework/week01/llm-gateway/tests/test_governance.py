"""治理能力：重试退避、fallback、熔断、限流、审计与用量。"""

from __future__ import annotations

import asyncio

import httpx

from tests.conftest import (
    ANSWER_SCHEMA,
    BACKUP_HOST,
    CHAT_KEY,
    PRIMARY_HOST,
    RetryConfig,
    build_harness,
    chat_completion,
    chat_error,
    simple_chat_request,
)


def _connect_errors(count: int) -> list[Exception]:
    return [httpx.ConnectError(f"down {index}") for index in range(count)]


async def test_retry_within_model_then_success(client: httpx.AsyncClient, harness) -> None:
    # 第一次连接失败 → 指数退避重试 → 同模型成功。
    harness.chat_transport.scripts[PRIMARY_HOST] = [
        httpx.ConnectError("transient"),
        chat_completion("recovered"),
    ]
    response = await client.post("/v1/chat", json=simple_chat_request())
    assert response.status_code == 200
    body = response.json()
    assert body["attempts"] == 2
    assert body["content"] == "recovered"


async def test_fallback_to_backup_model(client: httpx.AsyncClient, harness) -> None:
    # 主模型两次尝试均失败（达到每模型上限）→ 切备用模型。
    harness.chat_transport.scripts[PRIMARY_HOST] = _connect_errors(2)
    response = await client.post("/v1/chat", json=simple_chat_request())
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "general-backup"
    assert body["attempts"] == 3
    assert body["content"] == "hello from backup.test"


async def test_all_models_unavailable(client: httpx.AsyncClient, harness, admin_client) -> None:
    harness.chat_transport.scripts[PRIMARY_HOST] = [chat_error(429) for _ in range(2)]
    harness.chat_transport.scripts[BACKUP_HOST] = [chat_error(429) for _ in range(2)]
    response = await client.post("/v1/chat", json=simple_chat_request())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "model_unavailable"

    traces = (await admin_client.get("/v1/traces")).json()
    failed = traces["items"][0]
    assert failed["status"] == "failed"
    assert failed["error_code"] == "model_unavailable"
    assert failed["attempts"] == 4
    assert failed["actual_model"] is None
    assert failed["key_id"] == "test-chat"


async def test_circuit_breaker_opens_and_recovers(client: httpx.AsyncClient, admin_client, harness) -> None:
    # failure_threshold=2：主模型连续两个请求失败后熔断打开。
    harness.chat_transport.scripts[PRIMARY_HOST] = _connect_errors(4)  # 2 个请求 × 每请求 2 次尝试
    for _ in range(2):
        response = await client.post("/v1/chat", json=simple_chat_request())
        assert response.status_code == 200  # 备用模型兜底

    breaker_state = (await admin_client.get("/healthz")).json()["models"]["general-primary"]
    assert breaker_state == "open"

    # 熔断打开：主模型被跳过，请求直接由备用模型服务。
    response = await client.post("/v1/chat", json=simple_chat_request())
    assert response.json()["model"] == "general-backup"
    assert response.json()["attempts"] == 1

    # 冷却（0.2s）后进入半开，试探请求成功 → 恢复关闭。
    await asyncio.sleep(0.25)
    response = await client.post("/v1/chat", json=simple_chat_request())
    assert response.json()["model"] == "general-primary"
    breaker_state = (await admin_client.get("/healthz")).json()["models"]["general-primary"]
    assert breaker_state == "closed"


async def test_rate_limit_per_key(tmp_path, monkeypatch) -> None:
    harness = build_harness(tmp_path, monkeypatch, rpm=1)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness.app),
            base_url="http://t",
            headers={"authorization": f"Bearer {CHAT_KEY}"},
        ) as client:
            first = await client.post("/v1/chat", json=simple_chat_request())
            second = await client.post("/v1/chat", json=simple_chat_request())
    finally:
        await harness.app.state.gateway.aclose()
        harness.app.state.store.close()
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"


async def test_traces_and_usage_summary(client: httpx.AsyncClient, admin_client, harness) -> None:
    payload = simple_chat_request(
        response_schema=ANSWER_SCHEMA,
        prompt={"name": "qa_assistant", "version": "v1", "variables": {"persona": "客服"}},
    )
    harness.chat_transport.scripts[PRIMARY_HOST] = [chat_completion('{"answer": "ok"}')]
    response = await client.post("/v1/chat", json=payload)
    assert response.status_code == 200

    traces = (await admin_client.get("/v1/traces")).json()
    assert traces["total"] >= 1
    trace = traces["items"][0]
    assert trace["status"] == "success"
    assert trace["prompt_name"] == "qa_assistant"
    assert trace["prompt_version"] == "v1"
    assert trace["input_tokens"] == 5 and trace["output_tokens"] == 3
    # 成本 = (5×1.0 + 3×4.0)/1e6
    assert trace["cost_usd"] > 0

    usage_by_model = (await admin_client.get("/v1/usage", params={"group_by": "model"})).json()
    row = next(item for item in usage_by_model if item["group"] == "general-primary")
    assert row["requests"] >= 1
    assert row["input_tokens"] >= 5

    usage_by_key = (await admin_client.get("/v1/usage", params={"group_by": "key"})).json()
    assert any(item["group"] == "test-chat" for item in usage_by_key)


async def test_traces_pagination(admin_client) -> None:
    first = await admin_client.get("/v1/traces", params={"limit": 1})
    assert first.status_code == 200
    body = first.json()
    assert len(body["items"]) <= 1
    assert isinstance(body["total"], int)


async def test_retry_statuses_configurable(tmp_path, monkeypatch) -> None:
    # retry_statuses 只含 429：HTTP 500 不可重试，主模型一次失败立即切备模型。
    harness = build_harness(
        tmp_path,
        monkeypatch,
        retry=RetryConfig(
            max_attempts_per_model=2,
            base_delay=0.001,
            max_delay=0.005,
            total_deadline=5.0,
            retry_statuses=[429],
        ),
    )
    harness.chat_transport.scripts[PRIMARY_HOST] = [chat_error(500)]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness.app),
            base_url="http://t",
            headers={"authorization": f"Bearer {CHAT_KEY}"},
        ) as client:
            response = await client.post("/v1/chat", json=simple_chat_request())
    finally:
        await harness.app.state.gateway.aclose()
        harness.app.state.store.close()
    assert response.status_code == 200
    assert response.json()["model"] == "general-backup"
    assert response.json()["attempts"] == 2  # 主模型 1 次（不重试）+ 备模型 1 次


async def test_weighted_round_robin_rotates(client: httpx.AsyncClient) -> None:
    # fast-pool 与 general-backup 权重各 1：请求应在两者间交替。
    served = []
    for _ in range(4):
        response = await client.post("/v1/chat", json=simple_chat_request(model="fast-pool"))
        assert response.status_code == 200
        served.append(response.json()["model"])
    assert served == ["fast-pool", "general-backup", "fast-pool", "general-backup"]


async def test_cached_tokens_priced(client: httpx.AsyncClient, harness, admin_client) -> None:
    harness.chat_transport.scripts[PRIMARY_HOST] = [chat_completion("命中缓存", cached_tokens=3)]
    response = await client.post("/v1/chat", json=simple_chat_request(model="fast-pool"))
    assert response.status_code == 200
    assert response.json()["usage"]["cached_tokens"] == 3

    # fast-pool 定价 input=1.0 / cached_input=0.1 / output=4.0：
    # 5 输入（3 命中缓存）+ 3 输出 → (2*1.0 + 3*0.1 + 3*4.0) / 1e6
    traces = (await admin_client.get("/v1/traces")).json()
    assert traces["items"][0]["cached_tokens"] == 3
    assert traces["items"][0]["cost_usd"] == (2 * 1.0 + 3 * 0.1 + 3 * 4.0) / 1_000_000


async def test_stream_cancelled_on_client_disconnect(harness) -> None:
    # 直接驱动服务层：disconnect_check 返回 True 应停止事件流并记 cancelled。
    from app.schemas import ChatRequest, Message

    gateway = harness.app.state.gateway

    async def always_disconnected() -> bool:
        return True

    request = ChatRequest(model="general-primary", messages=[Message(role="user", content="hi")])
    _, events = gateway.start_stream_events(request, "test-chat", disconnect_check=always_disconnected)
    collected = [event async for _, event in events if event is not None]
    assert [event.type for event in collected] == ["response.started"]  # 首个数据块前即断开

    traces, _ = await gateway.store.list_traces()
    assert traces[0].status == "cancelled"
    assert traces[0].actual_model == "general-primary"


async def test_rate_limit_returns_retry_after(tmp_path, monkeypatch) -> None:
    harness = build_harness(tmp_path, monkeypatch, rpm=1)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=harness.app),
            base_url="http://t",
            headers={"authorization": f"Bearer {CHAT_KEY}"},
        ) as client:
            await client.post("/v1/chat", json=simple_chat_request())
            second = await client.post("/v1/chat", json=simple_chat_request())
    finally:
        await harness.app.state.gateway.aclose()
        harness.app.state.store.close()
    assert second.status_code == 429
    assert second.headers["retry-after"] == "1"

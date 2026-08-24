"""测试公共设施：脚本化上游传输 + 应用组装。

思路（课程 FakeProvider / test_gateway.py 的传输层版本）：
- ScriptedTransport 按上游 host 维护"响应或异常"队列，耗尽后回退默认响应；
- create_app(providers=...) 注入挂 MockTransport 的 Provider，全部离线可测。
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import (
    AuthConfig,
    AuthKeyConfig,
    BreakerConfig,
    DatabaseConfig,
    GatewayConfig,
    ModelConfig,
    PriceConfig,
    RetryConfig,
    StreamConfig,
)
from app.main import create_app
from app.services.upstream import OpenAIChatProvider, OpenAIResponsesProvider

PROJECT_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_DIR / "prompts"

CHAT_KEY = "gw-chat-test-key-0001"
ADMIN_KEY = "gw-admin-test-key-0001"
PRIMARY_HOST = "primary.test"
BACKUP_HOST = "backup.test"
OPENAI_HOST = "openai.test"


# ---------------------------------------------------------------------------
# Mock 响应构造
# ---------------------------------------------------------------------------


def chat_completion(
    content: str,
    *,
    input_tokens: int = 5,
    output_tokens: int = 3,
    cached_tokens: int = 0,
) -> httpx.Response:
    usage: dict[str, Any] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if cached_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "mock",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": usage,
        },
    )


def chat_error(status: int, *, error_type: str = "mock_error") -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": "mock failure", "type": error_type, "code": None}})


def chat_stream_sse(deltas: list[str], *, input_tokens: int = 5, output_tokens: int = 3) -> httpx.Response:
    lines: list[str] = []
    for delta in deltas:
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "mock",
            "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
        }
        lines.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    final = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "mock",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    lines.append(f"data: {json.dumps(final)}\n\n")
    lines.append("data: [DONE]\n\n")
    return httpx.Response(200, text="".join(lines), headers={"content-type": "text/event-stream"})


def slow_stream_sse(deltas: list[str], delay: float) -> httpx.Response:
    # 异步生成器响应体：制造上游慢流以触发心跳。
    async def body():
        for delta in deltas:
            await _sleep(delay)
            chunk = {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "mock",
                "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        final = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "mock",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        yield f"data: {json.dumps(final)}\n\ndata: [DONE]\n\n".encode()

    return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def responses_completion(text: str, *, input_tokens: int = 5, output_tokens: int = 3) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_test",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "mock",
            "output": [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            ],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": 8},
        },
    )


def responses_stream_sse(deltas: list[str], *, input_tokens: int = 5, output_tokens: int = 3) -> httpx.Response:
    lines: list[str] = []
    for delta in deltas:
        event = {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": delta,
        }
        lines.append(f"event: response.output_text.delta\ndata: {json.dumps(event, ensure_ascii=False)}\n\n")
    completed = {
        "type": "response.completed",
        "response": {
            "id": "resp_test",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "mock",
            "output": [],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": 8},
        },
    }
    lines.append(f"event: response.completed\ndata: {json.dumps(completed)}\n\n")
    return httpx.Response(200, text="".join(lines), headers={"content-type": "text/event-stream"})


class ScriptedTransport(httpx.AsyncBaseTransport):
    # scripts[host] 是响应/异常队列；耗尽后回退默认工厂（按请求体 stream 标志分流）。
    def __init__(self) -> None:
        self.scripts: dict[str, list[httpx.Response | Exception]] = defaultdict(list)
        self.defaults: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host or ""
        queue = self.scripts.get(host)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        factory = self.defaults.get(host)
        if factory is not None:
            return factory(request)
        try:
            body = json.loads(request.content) if request.content else {}
        except (ValueError, UnicodeDecodeError):
            body = {}
        if body.get("stream"):
            return chat_stream_sse(["默认", "流式", "回复"])
        return chat_completion(f"hello from {host}")


# ---------------------------------------------------------------------------
# Harness 组装
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    app: Any
    chat_transport: ScriptedTransport
    responses_transport: ScriptedTransport
    config: GatewayConfig


def make_config(
    tmp_path: Path,
    *,
    rpm: int = 1000,
    retry: RetryConfig | None = None,
    breaker: BreakerConfig | None = None,
    heartbeat_seconds: float | None = None,
) -> GatewayConfig:
    return GatewayConfig(
        auth=AuthConfig(
            keys=[
                AuthKeyConfig(key_id="test-chat", key=CHAT_KEY, scopes=["chat"], rpm=rpm),
                AuthKeyConfig(key_id="test-admin", key=ADMIN_KEY, scopes=["chat", "admin"], rpm=rpm),
            ]
        ),
        models={
            "general-primary": ModelConfig(
                provider_model="primary-model",
                base_url=f"https://{PRIMARY_HOST}/v1",
                api_key_env="GW_TEST_PRIMARY_KEY",
                protocol="chat_completions",
                structured_output_mode="json_object",
                fallbacks=["general-backup"],
                price=PriceConfig(input=1.0, output=4.0),
            ),
            "general-backup": ModelConfig(
                provider_model="backup-model",
                base_url=f"https://{BACKUP_HOST}/v1",
                api_key_env="GW_TEST_BACKUP_KEY",
                protocol="chat_completions",
                structured_output_mode="prompt_only",
                price=PriceConfig(input=0.8, output=3.2),
            ),
            "openai-responses": ModelConfig(
                provider_model="responses-model",
                base_url=f"https://{OPENAI_HOST}/v1",
                api_key_env="GW_TEST_OPENAI_KEY",
                protocol="responses",
                structured_output_mode="json_schema",
                price=PriceConfig(input=2.5, output=10.0),
            ),
            "plain-model": ModelConfig(
                provider_model="plain-model",
                base_url=f"https://{PRIMARY_HOST}/v1",
                api_key_env="GW_TEST_PRIMARY_KEY",
                protocol="chat_completions",
                structured_output_mode="none",
                price=PriceConfig(input=1.0, output=4.0),
            ),
            # 加权轮询池：fast-pool 与 general-backup 各权重 1，请求应交替由两者服务。
            "fast-pool": ModelConfig(
                provider_model="fast-pool-model",
                base_url=f"https://{PRIMARY_HOST}/v1",
                api_key_env="GW_TEST_PRIMARY_KEY",
                protocol="chat_completions",
                structured_output_mode="json_object",
                strategy="weighted_round_robin",
                weights={"fast-pool": 1, "general-backup": 1},
                fallbacks=["general-backup"],
                price=PriceConfig(input=1.0, output=4.0, cached_input=0.1),
            ),
        },
        retry=retry or RetryConfig(max_attempts_per_model=2, base_delay=0.001, max_delay=0.005, total_deadline=5.0),
        breaker=breaker or BreakerConfig(failure_threshold=2, cooldown_seconds=0.2),
        stream=StreamConfig(heartbeat_seconds=heartbeat_seconds or 15.0),
        prompts_dir=str(PROMPTS_DIR),
        database=DatabaseConfig(path=str(tmp_path / "gateway.db")),
    )


def build_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **config_overrides: Any,
) -> Harness:
    monkeypatch.setenv("GW_TEST_PRIMARY_KEY", "sk-primary")
    monkeypatch.setenv("GW_TEST_BACKUP_KEY", "sk-backup")
    monkeypatch.setenv("GW_TEST_OPENAI_KEY", "sk-openai")
    chat_transport = ScriptedTransport()
    responses_transport = ScriptedTransport()
    def openai_default(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("stream"):
            return responses_stream_sse(["默认", "回复"])
        return responses_completion("hello from responses")

    responses_transport.defaults[OPENAI_HOST] = openai_default
    config = make_config(tmp_path, **config_overrides)
    application = create_app(
        config,
        providers={
            "chat_completions": OpenAIChatProvider(transport=chat_transport),
            "responses": OpenAIResponsesProvider(transport=responses_transport),
        },
    )
    return Harness(application, chat_transport, responses_transport, config)


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return build_harness(tmp_path, monkeypatch)


@pytest.fixture
async def client(harness: Harness):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app),
        base_url="http://gateway.test",
        headers={"authorization": f"Bearer {CHAT_KEY}"},
    ) as async_client:
        yield async_client
    await harness.app.state.gateway.aclose()
    harness.app.state.store.close()


@pytest.fixture
async def admin_client(harness: Harness):
    # 独立客户端（admin Key）；不复用 chat 客户端，避免互相污染请求头。
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.app),
        base_url="http://gateway.test",
        headers={"authorization": f"Bearer {ADMIN_KEY}"},
    ) as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------


def chat_request_bodies(transport: ScriptedTransport) -> list[dict[str, Any]]:
    bodies = []
    for request in transport.requests:
        if request.url.path.endswith("/chat/completions"):
            bodies.append(json.loads(request.content))
    return bodies


def parse_sse(text: str) -> list[tuple[int, dict[str, Any]]]:
    # 解析 id/data 对；注释行（心跳）被跳过。
    events: list[tuple[int, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        seq = -1
        data: dict[str, Any] | None = None
        for line in block.split("\n"):
            if line.startswith("id: "):
                seq = int(line[4:])
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if data is not None:
            events.append((seq, data))
    return events


def simple_chat_request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "general-primary",
        "messages": [{"role": "user", "content": "你好"}],
    }
    payload.update(overrides)
    return payload


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

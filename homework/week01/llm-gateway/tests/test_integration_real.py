"""可选的真实上游集成测试（课程 test_gateway2.py 模式）。

设置 DEEPSEEK_API_KEY 后才会运行；离线环境下自动跳过。
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from app.config import (
    AuthConfig,
    AuthKeyConfig,
    DatabaseConfig,
    GatewayConfig,
    ModelConfig,
    PriceConfig,
)
from app.main import create_app
from app.services.upstream import OpenAIChatProvider

pytestmark = pytest.mark.skipif(not os.getenv("DEEPSEEK_API_KEY"), reason="需要 DEEPSEEK_API_KEY")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@pytest.fixture
def real_app(tmp_path: Path):
    config = GatewayConfig(
        auth=AuthConfig(
            keys=[AuthKeyConfig(key_id="real", key="gw-real-test-key-0001", scopes=["chat"], rpm=60)]
        ),
        models={
            "general-primary": ModelConfig(
                provider_model=os.getenv("REAL_PRIMARY_MODEL", "deepseek-chat"),
                base_url="https://api.deepseek.com",
                api_key_env="DEEPSEEK_API_KEY",
                structured_output_mode="json_object",
                price=PriceConfig(input=1.0, output=4.0),
            ),
        },
        prompts_dir=str(PROMPTS_DIR),
        database=DatabaseConfig(path=str(tmp_path / "real.db")),
    )
    return create_app(config, providers={"chat_completions": OpenAIChatProvider()})


async def test_real_deepseek_chat(real_app) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://t", timeout=60
    ) as client:
        response = await client.post(
            "/v1/chat",
            json={
                "model": "general-primary",
                "messages": [{"role": "user", "content": "用一句话解释 LLM Gateway"}],
            },
            headers={"authorization": "Bearer gw-real-test-key-0001"},
        )
    assert response.status_code == 200
    assert response.json()["content"]
    await real_app.state.gateway.aclose()
    real_app.state.store.close()

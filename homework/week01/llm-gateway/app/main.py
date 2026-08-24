"""应用工厂与生命周期组装。

create_app 同步完成全部组装（模板注册表、SQLite 建表、熔断器、限流器），
lifespan 只负责优雅关闭 —— 因此 httpx.ASGITransport 不触发 lifespan 也能完整测试。
运行：uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response

from app.api.compat import router as compat_router
from app.api.routes import router as api_router
from app.config import GatewayConfig, load_config
from app.core.errors import register_error_handlers
from app.core.ratelimit import FixedWindowRateLimiter
from app.services.gateway import GatewayService
from app.services.prompts import PromptRegistry
from app.services.router import CircuitBreaker, ModelRouter
from app.services.upstream import OpenAIChatProvider, OpenAIResponsesProvider, Provider
from app.services.usage import UsageStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app(
    config: GatewayConfig | None = None,
    providers: Mapping[str, Provider] | None = None,
) -> FastAPI:
    # providers 可注入：测试传 MockTransport 支撑的 Provider（课程 FakeProvider 思路）。
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        yield
        await app.state.gateway.aclose()
        app.state.store.close()

    app = FastAPI(title="Agent LLM Gateway", version="0.1.0", lifespan=lifespan)
    register_error_handlers(app)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Response:
        # 每个请求分配 request_id，错误体与响应头都可追溯。
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    if config is None:
        config = load_config()
    else:
        # 直接传入的配置（测试）：相对路径相对当前工作目录解析。
        config.resolve_relative_paths(Path.cwd())

    prompts = PromptRegistry.load(config.prompts_dir)
    store = UsageStore(config.database.path)
    provider_map: dict[str, Provider] = (
        dict(providers)
        if providers is not None
        else {
            "chat_completions": OpenAIChatProvider(),
            "responses": OpenAIResponsesProvider(),
        }
    )
    model_router = ModelRouter(config.models, CircuitBreaker(config.breaker))

    app.state.config = config
    app.state.rate_limiter = FixedWindowRateLimiter()
    app.state.store = store
    app.state.gateway = GatewayService(config, provider_map, model_router, prompts, store)

    app.include_router(api_router)
    app.include_router(compat_router)
    return app

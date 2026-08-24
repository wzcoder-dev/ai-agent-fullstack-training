"""per-key 固定窗口限流。

chat 调用入口按配置中 Key 的 rpm 做每分钟固定窗口计数；
状态在进程内存中，单副本语义（多副本需外置，见 DEVELOPMENT.md §15）。
"""

from __future__ import annotations

import time

from fastapi import Depends, Request

from app.core.auth import AuthContext, require_scopes
from app.core.errors import GatewayError


class FixedWindowRateLimiter:
    # key_id -> (窗口起点, 已用次数)。窗口按 60s 对齐。
    def __init__(self, window_seconds: float = 60.0) -> None:
        self._window_seconds = window_seconds
        self._windows: dict[str, tuple[int, int]] = {}

    def allow(self, key_id: str, limit: int) -> bool:
        window_id = int(time.monotonic() // self._window_seconds)
        start, count = self._windows.get(key_id, (window_id, 0))
        if start != window_id:
            start, count = window_id, 0
        if count >= limit:
            self._windows[key_id] = (start, count)
            return False
        self._windows[key_id] = (start, count + 1)
        return True


_chat_auth = require_scopes("chat")


def chat_rate_limit():
    # chat 入口的组合依赖工厂：鉴权 + 限流，返回调用方身份。
    async def dependency(
        request: Request,
        auth: AuthContext = Depends(_chat_auth),
    ) -> AuthContext:
        config = request.app.state.config
        key_config = next((k for k in config.auth.keys if k.key_id == auth.key_id), None)
        if key_config is not None:
            limiter: FixedWindowRateLimiter = request.app.state.rate_limiter
            if not limiter.allow(auth.key_id, key_config.rpm):
                raise GatewayError("rate_limited", f"超过每分钟 {key_config.rpm} 次限额", 429)
        return auth

    return dependency

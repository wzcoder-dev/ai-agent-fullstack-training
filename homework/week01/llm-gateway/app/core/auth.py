"""Bearer API Key 鉴权与 scope 校验。

网关客户端 Key 在配置 auth.keys 中声明（key_id + key + scopes），
与供应商密钥完全无关。scope 取值：chat（调用）、admin（管理接口）。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Request

from app.core.errors import GatewayError


@dataclass(frozen=True)
class AuthContext:
    # 通过鉴权后的调用方身份，key_id 用于限流与审计。
    key_id: str
    scopes: tuple[str, ...]


def require_scopes(*required: str):
    # 依赖工厂：校验 Bearer Key 存在、有效且具备所需 scope。
    async def dependency(request: Request) -> AuthContext:
        config = request.app.state.config
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise GatewayError("unauthorized", "缺少 Bearer 凭据", 401)
        token = token.strip()
        for key_config in config.auth.keys:
            if secrets.compare_digest(key_config.key, token):
                missing = set(required) - set(key_config.scopes)
                if missing:
                    raise GatewayError(
                        "insufficient_scope",
                        f"该 Key 缺少权限: {', '.join(sorted(missing))}",
                        403,
                    )
                return AuthContext(key_config.key_id, tuple(key_config.scopes))
        raise GatewayError("unauthorized", "API Key 无效", 401)

    return dependency

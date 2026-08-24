"""YAML + 环境变量配置。

约定：
- 供应商密钥只在配置里写环境变量名（api_key_env），值由进程启动时读取；
- 字符串支持 ${VAR} / ${VAR:默认值} 插值，缺失且无默认值时启动失败；
- 相对路径相对配置文件所在目录解析。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class AuthKeyConfig(BaseModel):
    # 网关客户端凭据：key_id 用于审计与限流，scopes 控制可访问的接口类别。
    key_id: str = Field(min_length=1, max_length=100)
    key: str = Field(min_length=8, max_length=200)
    scopes: list[Literal["chat", "admin"]] = Field(default_factory=lambda: ["chat"])
    rpm: int = Field(default=60, ge=1, le=1_000_000)


class AuthConfig(BaseModel):
    keys: list[AuthKeyConfig] = Field(min_length=1)


class PriceConfig(BaseModel):
    # 每百万 token 的美元单价；cached_input 未配置时缓存 token 按 input 价计。
    input: float = Field(default=0.0, ge=0)
    output: float = Field(default=0.0, ge=0)
    cached_input: float | None = Field(default=None, ge=0)


class ModelConfig(BaseModel):
    # 平台模型名映射到供应商模型、地址、密钥环境变量与结构化输出能力。
    provider_model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    protocol: Literal["chat_completions", "responses"] = "chat_completions"
    structured_output_mode: Literal["json_schema", "json_object", "prompt_only", "none"] = "json_object"
    # priority：请求模型 → fallbacks 顺序尝试；
    # weighted_round_robin：按 weights 在 [请求模型, *fallbacks] 池内加权轮询选首选，其余按序兜底。
    strategy: Literal["priority", "weighted_round_robin"] = "priority"
    weights: dict[str, int] = Field(default_factory=dict)
    fallbacks: list[str] = Field(default_factory=list)
    price: PriceConfig = Field(default_factory=PriceConfig)


class RetryConfig(BaseModel):
    # 非流式传输类错误的重试策略：每模型次数上限 + 指数退避 + 请求级总 deadline。
    # retry_statuses 决定哪些上游 HTTP 状态码可重试（超时/连接错误不受此限，始终可重试）。
    max_attempts_per_model: int = Field(default=2, ge=1, le=10)
    base_delay: float = Field(default=0.5, ge=0, le=60)
    max_delay: float = Field(default=8.0, ge=0, le=120)
    total_deadline: float = Field(default=20.0, gt=0, le=600)
    retry_statuses: list[int] = Field(default_factory=lambda: [408, 409, 429, 500, 502, 503, 504])


class BreakerConfig(BaseModel):
    # 熔断器：连续失败 N 次打开，冷却后半开试探。
    failure_threshold: int = Field(default=3, ge=1)
    cooldown_seconds: float = Field(default=30.0, gt=0)


class StructuredConfig(BaseModel):
    # 结构化输出修复循环：校验失败后追加修复消息重发的最多次数。
    max_repairs: int = Field(default=2, ge=0, le=5)


class StreamConfig(BaseModel):
    # 上游静默超过该秒数发送 keep-alive 心跳。
    heartbeat_seconds: float = Field(default=15.0, gt=0)


class DatabaseConfig(BaseModel):
    path: str = "data/gateway.db"


class GatewayConfig(BaseModel):
    # 网关全局配置，load_config() 加载 YAML 后经 Pydantic 校验。
    auth: AuthConfig
    models: dict[str, ModelConfig] = Field(min_length=1)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    structured: StructuredConfig = Field(default_factory=StructuredConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    prompts_dir: str = "prompts"
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    # 配置文件所在目录，load_config 填写；用于解析相对路径。
    base_dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def check_fallback_references(self) -> GatewayConfig:
        for name, model in self.models.items():
            for fallback in model.fallbacks:
                if fallback not in self.models:
                    raise ValueError(f"模型 {name} 的 fallback {fallback} 不在白名单中")
            for weight_name in model.weights:
                if weight_name not in self.models:
                    raise ValueError(f"模型 {name} 的 weights 引用了未知模型 {weight_name}")
        return self

    def resolve_relative_paths(self, base_dir: Path) -> None:
        # 相对路径统一相对配置文件目录解析，避免受进程工作目录影响。
        prompts = Path(self.prompts_dir)
        if not prompts.is_absolute():
            self.prompts_dir = str((base_dir / prompts).resolve())
        database = Path(self.database.path)
        if not database.is_absolute():
            self.database.path = str((base_dir / database).resolve())


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _interpolate_env(value: Any) -> Any:
    # 递归把 ${VAR} / ${VAR:默认值} 替换为环境变量值。
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            env_value = os.getenv(name)
            if env_value is not None:
                return env_value
            if default is not None:
                return default
            raise ValueError(f"配置引用的环境变量 {name} 未设置")

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {key: _interpolate_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(item) for item in value]
    return value


def load_config(path: str | Path | None = None) -> GatewayConfig:
    # 读取并校验配置；缺省时优先 GATEWAY_CONFIG，其次 gateway.yaml，最后样例文件。
    if path is None:
        env_path = os.getenv("GATEWAY_CONFIG")
        if env_path:
            path = Path(env_path)
        else:
            path = Path("gateway.yaml") if Path("gateway.yaml").exists() else Path("gateway.example.yaml")
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = GatewayConfig.model_validate(_interpolate_env(raw))
    base_dir = config.base_dir or config_path.resolve().parent
    config.resolve_relative_paths(base_dir)
    return config

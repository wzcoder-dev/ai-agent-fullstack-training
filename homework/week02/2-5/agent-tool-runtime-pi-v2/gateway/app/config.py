from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class RouteTarget:
    name: str
    litellm_model: str
    api_base: str
    api_key_env: str
    max_attempts: int
    input_per_million: float
    output_per_million: float


@dataclass(frozen=True)
class GatewayConfig:
    host: str
    port: int
    api_key_env: str
    database: Path
    routes: dict[str, tuple[RouteTarget, ...]]
    concurrency: int
    requests_per_minute: int
    timeout_seconds: float
    prompts_file: Path


def load_config(path: str | Path | None = None) -> GatewayConfig:
    config_path = Path(path or os.getenv("GATEWAY_CONFIG", "../config/gateway.yaml")).resolve()
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    routes = {
        alias: tuple(
            RouteTarget(
                name=item["name"],
                litellm_model=item["litellm_model"],
                api_base=item["api_base"],
                api_key_env=item["api_key_env"],
                max_attempts=int(item["max_attempts"]),
                input_per_million=float(item["input_per_million"]),
                output_per_million=float(item["output_per_million"]),
            )
            for item in targets
        )
        for alias, targets in raw["routing"].items()
    }
    return GatewayConfig(
        host=raw["server"]["host"],
        port=int(raw["server"]["port"]),
        api_key_env=raw["server"]["api_key_env"],
        database=(config_path.parent / raw["server"]["database"]).resolve(),
        routes=routes,
        concurrency=int(raw["limits"]["concurrency"]),
        requests_per_minute=int(raw["limits"]["requests_per_minute"]),
        timeout_seconds=float(raw["limits"]["timeout_seconds"]),
        prompts_file=(config_path.parent / raw["prompts_file"]).resolve(),
    )

"""模型路由：白名单、fallback 链与每模型熔断器。

对应课程 1-6 的 MODEL_CONFIGS 白名单与 call_with_fallback 路由链；
熔断器与加权轮询策略为作业扩展（吸收自课程 1-7 的 weighted_round_robin）。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass

from app.config import BreakerConfig, ModelConfig
from app.core.errors import GatewayError
from app.schemas import ModelInfo

_CIRCUIT_STATES = ("closed", "open", "half_open")


class CircuitBreaker:
    # 每模型独立的三态熔断器；仅传输类"调用失败"计数，语义失败不计。
    def __init__(self, config: BreakerConfig) -> None:
        self._config = config
        self._states: dict[str, dict[str, float | str | int]] = {}

    def _entry(self, model: str) -> dict[str, float | str | int]:
        return self._states.setdefault(
            model,
            {"state": "closed", "failures": 0, "opened_at": 0.0},
        )

    def allows(self, model: str) -> bool:
        # open 且冷却已过 → 迁移到 half_open 并放行试探请求。
        entry = self._entry(model)
        if entry["state"] == "open":
            elapsed = time.monotonic() - float(entry["opened_at"])
            if elapsed >= self._config.cooldown_seconds:
                entry["state"] = "half_open"
                return True
            return False
        return True  # closed / half_open 均放行

    def record_success(self, model: str) -> None:
        entry = self._entry(model)
        entry["state"] = "closed"
        entry["failures"] = 0

    def record_failure(self, model: str) -> None:
        entry = self._entry(model)
        if entry["state"] == "half_open":
            entry["state"] = "open"
            entry["opened_at"] = time.monotonic()
            return
        failures = int(entry["failures"]) + 1
        entry["failures"] = failures
        if failures >= self._config.failure_threshold:
            entry["state"] = "open"
            entry["opened_at"] = time.monotonic()

    def states(self) -> dict[str, str]:
        # 供 /healthz 暴露；未记录的模型视为 closed。
        known = set(self._states)
        result = {model: str(self._states[model]["state"]) for model in known}
        return result if result else {}


@dataclass(frozen=True)
class ModelEntry:
    # 路由链节点：平台模型名 + 供应商配置。
    name: str
    config: ModelConfig


class ModelRouter:
    # 白名单注册表 + fallback 链构建 + 加权轮询 + 定价。
    def __init__(self, models: dict[str, ModelConfig], breaker: CircuitBreaker) -> None:
        self.models = models
        self.breaker = breaker
        self._counters: dict[str, itertools.count] = {}

    def build_chain(self, requested: str, require_structured: bool) -> list[ModelEntry]:
        # 请求模型 → fallbacks 递归展开去重；能力过滤保证 fallback 等价。
        names: list[str] = []
        visited: set[str] = set()
        queue = [requested]
        while queue:
            name = queue.pop(0)
            if name in visited:
                continue
            visited.add(name)
            config = self.models.get(name)
            if config is None:
                if name == requested:
                    raise GatewayError("unknown_model", "模型不在 Gateway 允许列表中", 400)
                continue  # 未知的中间 fallback 节点直接跳过
            if require_structured and config.structured_output_mode == "none":
                if name == requested:
                    raise GatewayError("structured_output_unsupported", "模型不支持 Structured Output", 400)
                continue  # 不等价的 fallback 不进入路由链
            names.append(name)
            queue.extend(config.fallbacks)
        if not names:
            raise GatewayError("unknown_model", "模型不在 Gateway 允许列表中", 400)
        names = self._apply_strategy(requested, names)
        return [ModelEntry(name=name, config=self.models[name]) for name in names]

    def _apply_strategy(self, requested: str, names: list[str]) -> list[str]:
        # priority 保持原顺序；weighted_round_robin 按 weights 在池内轮询选首选，
        # 其余成员按原顺序跟在后面作为 fallback（吸收自课程 1-7）。
        strategy = self.models[requested].strategy
        if strategy != "weighted_round_robin" or len(names) < 2:
            return names
        weights = self.models[requested].weights
        weighted = [name for name in names for _ in range(max(1, weights.get(name, 1)))]
        counter = self._counters.setdefault(requested, itertools.count())
        primary = weighted[next(counter) % len(weighted)]
        return [primary, *[name for name in names if name != primary]]

    def cost_usd(self, model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        # 缓存命中的输入 token 按 cached_input 价计（未配置则同 input 价）。
        price = self.models[model].price
        cached = min(cached_tokens, input_tokens)
        fresh = input_tokens - cached
        cached_rate = price.cached_input if price.cached_input is not None else price.input
        return (fresh * price.input + cached * cached_rate + output_tokens * price.output) / 1_000_000

    def model_infos(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                name=name,
                provider_model=config.provider_model,
                protocol=config.protocol,
                structured_output_mode=config.structured_output_mode,
                fallbacks=list(config.fallbacks),
                price_input_per_million=config.price.input,
                price_output_per_million=config.price.output,
            )
            for name, config in sorted(self.models.items())
        ]

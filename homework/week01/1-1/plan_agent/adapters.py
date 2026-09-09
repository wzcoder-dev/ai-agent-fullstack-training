"""模型适配层。

验收要求「模型可替换：DeepSeek ↔ 别家 Adapter，Loop 不变」，做法是
Loop 只依赖 ModelAdapter 协议（一个 complete 方法）；各家的参数差异
（base_url、默认模型、私有参数）都封装在各自 Adapter 里。换模型 =
换 Adapter 配置，Loop 代码一行不改。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Protocol

from openai import OpenAI

LLM_ERROR_KINDS = ("rate_limit", "timeout", "refusal", "api")


class LLMError(Exception):
    """统一分类的模型调用失败，Loop 据此决定重试还是停止。

    kind:
    - rate_limit: 429 等限流，可退避重试
    - timeout: 超时 / 连接失败，可退避重试
    - refusal: 模型拒答（content_filter），不可重试，立即停止
    - api: 其余 API 错误（含空响应），可退避重试
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"[{kind}] {message}")
        if kind not in LLM_ERROR_KINDS:
            raise ValueError(f"未知错误类型: {kind!r}")
        self.kind = kind
        self.message = message


class ModelAdapter(Protocol):
    """Loop 依赖的最小协议：给消息，回模型原始文本。"""

    name: str

    def complete(self, messages: list[dict]) -> str: ...


def _classify_error(exc: Exception) -> LLMError:
    """把 OpenAI SDK 的异常映射成统一分类。"""
    import openai

    if isinstance(exc, openai.RateLimitError):
        return LLMError("rate_limit", f"上游限流(429): {exc}")
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return LLMError("timeout", f"超时或连接失败: {exc}")
    if isinstance(exc, openai.APIStatusError):
        return LLMError("api", f"API 错误(status={exc.status_code}): {exc}")
    return LLMError("api", f"未知 API 错误: {exc}")


class _OpenAICompatAdapter:
    """OpenAI 兼容 Chat Completions 的公共实现，子类只声明供应商差异。"""

    name: str
    default_model: str
    base_url: str
    api_key_env: str

    def __init__(self, *, model: str | None = None, api_key_env: str | None = None) -> None:
        self.model = model or self.default_model
        self.api_key_env = api_key_env or self.api_key_env
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"缺少 API key：请先 export {self.api_key_env}=<你的密钥>（或在项目目录 .env 中配置）"
            )
        self._client = OpenAI(api_key=api_key, base_url=self.base_url)

    def _create(self, messages: list[dict]) -> dict:
        """各家私有参数差异放在这里，由子类覆写；默认无额外参数。"""
        return {"model": self.model, "messages": messages}

    def complete(self, messages: list[dict]) -> str:
        try:
            response = self._client.chat.completions.create(**self._create(messages))
        except Exception as exc:  # noqa: BLE001 —— SDK 异常族太广，统一归类后再抛
            raise _classify_error(exc) from exc
        choice = response.choices[0]
        if choice.finish_reason == "content_filter":
            raise LLMError("refusal", "模型拒答（finish_reason=content_filter）")
        text = choice.message.content or ""
        if not text.strip():
            raise LLMError("api", "模型返回了空内容")
        return text


class DeepSeekAdapter(_OpenAICompatAdapter):
    name = "deepseek"
    default_model = "deepseek-v4-flash"
    base_url = "https://api.deepseek.com"
    api_key_env = "DEEPSEEK_API_KEY"

    def _create(self, messages: list[dict]) -> dict:
        # 沿用课程代码的调用方式：关闭思考，降低延迟
        return {
            "model": self.model,
            "messages": messages,
            "reasoning_effort": "low",
            "extra_body": {"thinking": {"type": "disabled"}},
        }


class ZhipuAdapter(_OpenAICompatAdapter):
    """智谱 GLM，OpenAI 兼容端点。默认模型 GLM-5.3-Flash，可用 --model 覆盖。"""

    name = "zhipu"
    default_model = "GLM-5.3-Flash"
    base_url = "https://open.bigmodel.cn/api/paas/v4/"
    api_key_env = "ZHIPU_API_KEY"


class FakeAdapter:
    """按脚本依次返回 canned 响应或抛出异常，单测与离线评测用，不访问网络。"""

    def __init__(self, responses: Sequence[str | Exception]) -> None:
        self.name = "fake"
        self.model = "fake-model"
        self._responses: list[str | Exception] = list(responses)
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("FakeAdapter 的响应用尽了，脚本与预期调用次数不符")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


PROVIDERS = {
    "deepseek": DeepSeekAdapter,
    "zhipu": ZhipuAdapter,
}


def build_adapter(provider: str, model: str | None = None) -> ModelAdapter:
    """CLI 入口用的工厂：fake 之外的名字必须在 PROVIDERS 里。"""
    if provider == "fake":
        raise ValueError("fake 适配器需要脚本化响应，请直接构造 FakeAdapter")
    if provider not in PROVIDERS:
        raise ValueError(f"未知 provider: {provider!r}，可选：{', '.join(PROVIDERS)}")
    return PROVIDERS[provider](model=model)

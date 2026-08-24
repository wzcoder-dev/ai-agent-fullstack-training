"""API Pydantic 模型与 SSE 事件模型。

请求模型一律 extra="forbid"，未知字段在入口即被 422 拒绝；
响应模型作为 FastAPI response_model 出口契约。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Message(BaseModel):
    # 跨模型通用的单条对话消息，隔离供应商消息格式差异。
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)


class PromptSelection(BaseModel):
    # 只允许调用方选择受控模板及变量，不能提交或覆盖模板正文。
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=50)
    variables: dict[str, str] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    # 统一请求协议，并在入口拦截不合法组合。
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=100)
    messages: list[Message] = Field(min_length=1, max_length=100)
    stream: bool = False
    response_schema: dict[str, Any] | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    prompt: PromptSelection | None = None

    @model_validator(mode="after")
    def check_supported_combination(self) -> ChatRequest:
        if self.stream and self.response_schema is not None:
            raise ValueError("stream 与 response_schema 不能同时使用")
        return self


class Usage(BaseModel):
    # 统一输入与输出 token 统计口径；cached_tokens 为供应商返回的缓存命中输入 token。
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(default=0, ge=0)


class SamplingParams(BaseModel):
    # 采样参数（课程 1-2）：透传给上游，未设置的字段不进入请求体。
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)


class ChatResponse(BaseModel):
    # 统一非流式调用结果。
    model_config = ConfigDict(extra="forbid")

    request_id: str
    model: str
    content: str
    parsed: Any = None
    usage: Usage
    latency_ms: int = Field(ge=0)
    attempts: int = Field(ge=1)
    finish_reason: str | None = None


class ResponsesRequest(BaseModel):
    # Responses 风格薄入口协议，内部归一为 ChatRequest 走统一链路。
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=100)
    input: str = Field(min_length=1, max_length=20_000)
    instructions: str | None = Field(default=None, min_length=1, max_length=8_000)
    response_schema: dict[str, Any] | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    prompt: PromptSelection | None = None


class CallTrace(BaseModel):
    # 单次调用审计记录；默认不保存 Prompt 与模型回答正文。
    model_config = ConfigDict(extra="forbid")

    request_id: str
    timestamp: datetime
    key_id: str
    requested_model: str
    actual_model: str | None = None
    prompt_name: str | None = None
    prompt_version: str | None = None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(ge=0)
    latency_ms: int = Field(ge=0)
    ttft_ms: int | None = Field(default=None, ge=0)
    attempts: int = Field(ge=0)
    status: Literal["success", "failed", "cancelled"]
    error_code: str | None = None


class PromptTemplateInfo(BaseModel):
    # 模板资产的公开元数据；template_sha256 为模板正文指纹。
    name: str
    version: str
    description: str | None = None
    variables: list[str]
    template_sha256: str


class RenderPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variables: dict[str, str] = Field(default_factory=dict)


class RenderPreviewResponse(BaseModel):
    system_message: str
    sha256: str


class ModelInfo(BaseModel):
    # /v1/models 暴露的白名单信息，不含任何密钥。
    name: str
    provider_model: str
    protocol: Literal["chat_completions", "responses"]
    structured_output_mode: Literal["json_schema", "json_object", "prompt_only", "none"]
    fallbacks: list[str]
    price_input_per_million: float
    price_output_per_million: float


class UsageSummaryRow(BaseModel):
    # 用量聚合行：group 为模型名或 key_id。
    group: str
    requests: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)


# ---------------------------------------------------------------------------
# SSE 事件模型：每个事件有 type 判别字段，seq 由编码器统一分配并写入 id: 行。
# ---------------------------------------------------------------------------


class ResponseStarted(BaseModel):
    request_id: str
    model: str
    type: Literal["response.started"] = "response.started"


class ContentDelta(BaseModel):
    seq: int
    delta: str
    type: Literal["content.delta"] = "content.delta"


class ResponseCompleted(BaseModel):
    model: str
    usage: Usage
    ttft_ms: int | None = None
    latency_ms: int = Field(ge=0)
    attempts: int = Field(ge=1)
    finish_reason: str | None = None
    type: Literal["response.completed"] = "response.completed"


class ResponseFailed(BaseModel):
    error_code: str
    message: str
    type: Literal["response.failed"] = "response.failed"

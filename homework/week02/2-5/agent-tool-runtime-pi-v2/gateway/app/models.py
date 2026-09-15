from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatCompletionRequest(BaseModel):
    """Subset used by pi; extra OpenAI-compatible fields pass through to LiteLLM."""

    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, max_length=100)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=32768)


class PromptRenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str | None = None
    variables: dict[str, str]


class OrderStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(pattern=r"^ORD-[A-Z]-[0-9]{4,12}$")


class OrderStatusOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: str
    status: str
    source: str
    customer_email: str | None = None

"""Tool Runtime 框架：Registry（库存）→ Snapshot（快照）→ invoke（五检查点）。

9.2 七条验收标准全部在本文件落地：
1  非法参数不进 handler          → 检查点 2
2  权限不足不产生副作用          → 检查点 3a
3  模型伪造 approved 不能绕过    → 检查点 3b（审批只认 context.approved_call_ids）
4  执行阶段不读 Registry 最新版  → 检查点 1（工具实现只从快照取；仅启用开关走 Registry 即时查询）
5  成功失败都保留原 tool_call_id → _finish 统一出口
6  非幂等写超时不自动重试        → 检查点 4（重试前先看 idempotent）
7  内部异常归一化、不泄漏堆栈    → 检查点 4b
"""

import asyncio
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_RETRIES = 2  # 仅幂等工具可享受；非幂等写操作 0 次


# ── 协议类型 ──


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExecutionContext(StrictModel):
    """可信上下文：Harness 注入。权限与审批只认这里，模型字段不参与。"""

    user_id: str
    tenant_id: str
    permissions: frozenset[str]
    approved_call_ids: frozenset[str] = frozenset()
    trace_id: str = ""


class ToolCall(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments_json: str  # 模型吐出的原始文本，解析权在 Runtime


class ToolResultMessage(StrictModel):
    role: Literal["tool"] = "tool"
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


class ToolError(StrictModel):
    code: Literal[
        "INVALID_ARGUMENT",
        "TOOL_NOT_FOUND",
        "PERMISSION_DENIED",
        "APPROVAL_REQUIRED",
        "TIMEOUT",
        "UPSTREAM_ERROR",
        "TOOL_DISABLED",
        "INTERNAL_ERROR",
        "INVALID_OUTPUT",
    ]
    message: str
    retryable: bool = False


# ── 工具定义与安全投影 ──


@dataclass(frozen=True)
class ToolDefinition:
    """完整契约（服务端）：治理 + 策略 + 实现；模型只见 provider_tool() 投影。"""

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    permission: str
    risk: Literal["low", "medium", "high"]
    timeout_seconds: float
    idempotent: bool  # False 的写操作：超时绝不自动重试
    handler: Callable[[BaseModel, ExecutionContext], Awaitable[BaseModel]]

    def provider_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }


# ── Registry：库存（有哪些 name@version、启停开关） ──


class Registry:
    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], ToolDefinition] = {}
        self._latest: dict[str, str] = {}
        self._enabled: dict[str, bool] = {}

    def register(self, tool: ToolDefinition, version: str = "v1") -> None:
        key = (tool.name, version)
        if key in self._tools:
            raise ValueError(f"工具重复注册: {key}")
        self._tools[key] = tool
        self._latest[tool.name] = version
        self._enabled.setdefault(tool.name, True)

    def get_latest(self, name: str) -> ToolDefinition | None:
        version = self._latest.get(name)
        return self._tools.get((name, version)) if version else None

    def set_enabled(self, name: str, enabled: bool) -> None:
        self._enabled[name] = enabled

    def is_enabled(self, name: str) -> bool:
        return self._enabled.get(name, False)

    def snapshot(self, routes: list[str]) -> "Snapshot":
        """冻结本轮工具：记录的是 ToolDefinition 对象本身（frozen dataclass），
        之后 Registry 升级版本也改不了它——快照隔离的根据。"""
        tools = {}
        for name in routes:
            tool = self.get_latest(name)
            if tool is not None:
                tools[name] = tool
        return Snapshot(tools)


# ── Snapshot：本轮冻结 ──


class Snapshot:
    def __init__(self, tools: dict[str, ToolDefinition]) -> None:
        self._tools = MappingProxyType(dict(tools))

    def find(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def provider_tools(self) -> list[dict]:
        return [tool.provider_tool() for tool in self._tools.values()]


# ── ToolRuntime：五检查点 ──


class ToolRuntime:
    def __init__(
        self, registry: Registry, audit: Callable[[dict], None] | None = None
    ) -> None:
        self._registry = registry
        self._audit = audit or (lambda event: None)

    async def invoke(
        self, snapshot: Snapshot, call: ToolCall, context: ExecutionContext
    ) -> ToolResultMessage:
        # ── 检查点 1：从 Snapshot 查找（只读快照，不读 Registry 的实现与版本）──
        tool = snapshot.find(call.name)
        if tool is None:
            return self._finish(call, context, ToolError(
                code="TOOL_NOT_FOUND", message=f"工具未开放: {call.name}"
            ))
        # 启用开关是治理杀开关，即时生效；它不是"实现版本"，不违反快照隔离
        if not self._registry.is_enabled(call.name):
            return self._finish(call, context, ToolError(
                code="TOOL_DISABLED", message=f"工具已停用: {call.name}"
            ))

        # ── 检查点 2：参数校验（先参数、后策略）──
        try:
            args = tool.input_model.model_validate_json(call.arguments_json)
        except ValidationError as exc:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in exc.errors()
            )
            return self._finish(call, context, ToolError(
                code="INVALID_ARGUMENT", message=f"参数校验失败: {detail}"
            ))

        # ── 检查点 3a：权限——和 context.permissions 比对，模型字段不参与 ──
        if tool.permission not in context.permissions:
            return self._finish(call, context, ToolError(
                code="PERMISSION_DENIED",
                message=f"缺少权限 {tool.permission}",
            ))
        # ── 检查点 3b：审批——高危操作只认 context.approved_call_ids ──
        # 模型即便在参数里自带 approved=true 也没用：Schema extra=forbid 根本收不进去
        if tool.risk == "high" and call.id not in context.approved_call_ids:
            return self._finish(call, context, ToolError(
                code="APPROVAL_REQUIRED",
                message="高危操作需要审批（审批记录来自可信上下文）",
            ))

        # ── 检查点 4：受控执行（超时预算 + 幂等决定重试 + 异常归一化）──
        attempts = 0
        while True:
            attempts += 1
            try:
                output = await asyncio.wait_for(
                    tool.handler(args, context), timeout=tool.timeout_seconds
                )
                break
            except asyncio.TimeoutError:
                # 非幂等写：超时可能业务已生效，绝不能盲试 → 直接 TIMEOUT
                if tool.idempotent and attempts <= MAX_RETRIES:
                    self._audit({
                        "event": "retry", "call_id": call.id,
                        "tool": call.name, "attempt": attempts,
                    })
                    continue
                return self._finish(call, context, ToolError(
                    code="TIMEOUT",
                    message=f"执行超时（重试 {attempts - 1} 次后放弃）"
                    if attempts > 1 else "执行超时",
                    retryable=tool.idempotent,
                ))
            except Exception:
                # ── 检查点 4b：内部异常归一化；细节不回模型、不进审计（只进服务日志）──
                return self._finish(call, context, ToolError(
                    code="INTERNAL_ERROR", message="内部错误，请稍后重试或联系管理员",
                ))

        # ── 检查点 5：结果包装（Output Schema 验收 → 带原 ID 的 ToolResultMessage）──
        try:
            validated = tool.output_model.model_validate(output)
        except ValidationError:
            return self._finish(call, context, ToolError(
                code="INVALID_OUTPUT",
                message="工具违反了自身返回协议，请记录工程故障",
            ))
        return self._finish(call, context, output=validated)

    def _finish(
        self,
        call: ToolCall,
        context: ExecutionContext,
        error: ToolError | None = None,
        output: BaseModel | None = None,
    ) -> ToolResultMessage:
        """统一出口：成功与失败都必须保留原始 tool_call_id（验收 #5）。"""
        if error is not None:
            result = ToolResultMessage(
                tool_call_id=call.id,
                name=call.name,
                content=json.dumps(
                    {"error": error.model_dump(exclude_none=True)}, ensure_ascii=False
                ),
                is_error=True,
            )
            outcome = error.code
        else:
            result = ToolResultMessage(
                tool_call_id=call.id,
                name=call.name,
                content=output.model_dump_json(),  # type: ignore[union-attr]
            )
            outcome = "OK"
        self._audit({
            "event": "invoke", "call_id": call.id, "tool": call.name,
            "user": context.user_id, "trace_id": context.trace_id,
            "outcome": outcome,
        })
        return result

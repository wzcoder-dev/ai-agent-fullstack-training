"""2-4：ToolRuntime.invoke() 空骨架（控制流完整，检查点待填充）。

需求 → 检查点 → 扩展点对照：

| 需求                              | 检查点 | 填哪里                              |
| --------------------------------- | ------ | ----------------------------------- |
| 1 根据工具名查找定义               | CP1    | `_find_definition`                  |
| 2 Pydantic 校验并结构化错误        | CP2    | `_validate_arguments`               |
| 3 PermissionEngine 三态决策        | CP3    | `_decide_permission`（只调用引擎）  |
| 4 deny/confirm 时 handler 零调用   | CP3'   | `invoke` 的 match 分支（结构性保证）|
| 5 只读/幂等驱动超时与重试          | CP4    | `_execute_governed` / `_should_retry` |
| 6 finalize 脱敏与审计              | CP5    | `_finalize`（只调用 redactor/auditor）|
| 7 返回结果保留原始 tool_call_id    | CP6    | `_wrap_result`（已实现：唯一不变量）  |

填充后的验收清单（建议写成 pytest，副作用探针 probe 记录 handler 进入）：
- probe == [] 当 decision=DENY / CONFIRM；
- 幂等读超时重试 ≤ policy.max_retries 且最终成功；非幂等写超时 probe 恰好 1 次；
- INTERNAL_ERROR 的 content 不含异常类名与消息；长度 ≤ policy.result_max_bytes；
- 成功与失败路径 result.tool_call_id 均 == call.id。

运行：.venv/bin/python tool_runtime_skeleton.py（演示控制流走到第一个未填充点）
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from tool_contracts import (
    ExecutionContext,
    StrictModel,
    ToolDefinition,
    ToolPolicy,
)

# ── 运行层协议类型 ──


class ToolCall(StrictModel):
    id: str
    name: str
    arguments_json: str  # 模型输出的原始文本，解析权在 Runtime


class ToolResultMessage(StrictModel):
    role: Literal["tool"] = "tool"
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


class ToolError(StrictModel):
    code: Literal[
        "TOOL_NOT_FOUND",
        "INVALID_ARGUMENT",
        "PERMISSION_DENIED",
        "APPROVAL_REQUIRED",
        "TIMEOUT",
        "INTERNAL_ERROR",
        "INVALID_OUTPUT",
    ]
    message: str
    retryable: bool = False


class PermissionVerdict(StrEnum):
    """PermissionEngine 的三态：放行 / 拒绝 / 需人工确认。"""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    verdict: PermissionVerdict
    reason: str = ""
    audit: Mapping[str, Any] = field(default_factory=dict)


# ── 执法者接口（只声明，不实现——见 2-4 要求 5） ──


class PermissionEngine(Protocol):
    def decide(
        self,
        definition: ToolDefinition,
        arguments: BaseModel,
        context: ExecutionContext,
    ) -> PermissionDecision: ...


class Redactor(Protocol):
    def redact(self, payload: str, policy: ToolPolicy) -> str:
        """脱敏 + 限长（≤ policy.result_max_bytes）。"""
        ...


class Auditor(Protocol):
    def record(self, event: Mapping[str, Any]) -> None: ...


# ── 骨架 ──


class ToolRuntime:
    """五段式控制流：查找 → 校验 → 裁决 → 受控执行 → 定稿。

    执法者（engine/redactor/auditor）全部注入；本类只编排，不裁决。
    """

    def __init__(
        self,
        engine: PermissionEngine,
        auditor: Auditor,
        redactor: Redactor,
    ) -> None:
        self._engine = engine
        self._auditor = auditor
        self._redactor = redactor

    async def invoke(
        self,
        definitions: Mapping[str, ToolDefinition],
        call: ToolCall,
        context: ExecutionContext,
    ) -> ToolResultMessage:
        # ── CP1：根据工具名查找定义 ──
        definition = self._find_definition(definitions, call)

        # ── CP2：Pydantic 校验，失败转结构化错误 ──
        arguments = self._validate_arguments(definition, call)

        # ── CP3：三态裁决；DENY/CONFIRM 在此提前返回，handler 零调用 ──
        decision = await self._decide_permission(definition, arguments, context)
        match decision.verdict:
            case PermissionVerdict.DENY:
                return self._wrap_result(call, error=ToolError(
                    code="PERMISSION_DENIED", message=decision.reason,
                ))
            case PermissionVerdict.CONFIRM:
                return self._wrap_result(call, error=ToolError(
                    code="APPROVAL_REQUIRED", message=decision.reason,
                ))
            case PermissionVerdict.ALLOW:
                pass  # 继续执行

        # ── CP4：受控执行（超时与重试由 policy 的只读/幂等属性驱动）──
        output = await self._execute_governed(definition, arguments, context)

        # ── CP5：finalize 脱敏与审计，随后 CP6 包装返回 ──
        return self._finalize(definition, call, context, output)

    # ── 扩展点（每个 stub 标注了填充提示）──

    def _find_definition(
        self,
        definitions: Mapping[str, ToolDefinition],
        call: ToolCall,
    ) -> ToolDefinition:
        """CP1。TODO：按 call.name 查 definitions；查不到返回
        TOOL_NOT_FOUND 的 ToolResultMessage（同样要带 call.id）。"""
        raise NotImplementedError("CP1: 按工具名查找定义")

    def _validate_arguments(
        self, definition: ToolDefinition, call: ToolCall
    ) -> BaseModel:
        """CP2。TODO：definition.input_model.model_validate_json(call.arguments_json)；
        ValidationError → INVALID_ARGUMENT 结构化错误（含字段级 detail），
        不把裸异常抛给 Loop。"""
        raise NotImplementedError("CP2: Pydantic 校验并结构化错误")

    async def _decide_permission(
        self,
        definition: ToolDefinition,
        arguments: BaseModel,
        context: ExecutionContext,
    ) -> PermissionDecision:
        """CP3。TODO：return self._engine.decide(definition, arguments, context)。
        本类不实现引擎——裁决依据是 policy（要求）对照 context（资格）。"""
        raise NotImplementedError("CP3: 调用 PermissionEngine 三态裁决")

    async def _execute_governed(
        self,
        definition: ToolDefinition,
        arguments: BaseModel,
        context: ExecutionContext,
    ) -> BaseModel:
        """CP4。TODO：asyncio.wait_for(handler(args, context), policy.timeout_seconds)；
        超时后用 _should_retry 决定是否重试；未捕获异常 → INTERNAL_ERROR
        （不外泄类名与消息）。"""
        raise NotImplementedError("CP4: 超时预算 + 幂等驱动的受控执行")

    def _should_retry(
        self, policy: ToolPolicy, attempt: int, error: ToolError
    ) -> bool:
        """CP4b。TODO：只有幂等（且通常只读）工具允许重试：
        attempt <= policy.max_retries 且 error.retryable。
        非幂等写操作恒 False——超时可能业务已生效。"""
        raise NotImplementedError("CP4b: 重试边界")

    def _finalize(
        self,
        definition: ToolDefinition,
        call: ToolCall,
        context: ExecutionContext,
        output: BaseModel,
    ) -> ToolResultMessage:
        """CP5。TODO：redactor.redact(payload, policy.result_max_bytes) →
        auditor.record({call_id, tool, user, trace_id, outcome}) →
        output 先过 Output Schema 校验（违约定 → INVALID_OUTPUT）→ _wrap_result。"""
        raise NotImplementedError("CP5: 脱敏、审计与结果定稿")

    # ── 已实现：唯一结构性不变量 ──

    def _wrap_result(
        self,
        call: ToolCall,
        error: ToolError | None = None,
        payload: str | None = None,
    ) -> ToolResultMessage:
        """CP6。无论成功失败，结果都带回原始 tool_call_id——
        这是唯一不允许被填充修改的不变量。"""
        if error is not None:
            import json

            return ToolResultMessage(
                tool_call_id=call.id,
                name=call.name,
                content=json.dumps({"error": error.model_dump()}, ensure_ascii=False),
                is_error=True,
            )
        assert payload is not None
        return ToolResultMessage(
            tool_call_id=call.id, name=call.name, content=payload
        )


# ── 演示：控制流走到第一个未填充检查点 ──

if __name__ == "__main__":
    import asyncio

    from tool_contracts import ExecutionMode

    async def main() -> None:
        runtime = ToolRuntime(
            engine=None,  # type: ignore[argtype]  # 引擎按作业要求不实现
            auditor=None,  # type: ignore[argtype]
            redactor=None,  # type: ignore[argtype]
        )
        context = ExecutionContext(
            trace_id="trace_42",
            user_id="user_demo",
            tenant_id="tenant_demo",
            permissions=frozenset({"order:read"}),
            allowed_tools=frozenset({"orders.get_order"}),
            mode=ExecutionMode.ENFORCE,
        )
        call = ToolCall(
            id="call_demo_1",
            name="orders.get_order",
            arguments_json='{"order_id": "ord_1001"}',
        )
        try:
            await runtime.invoke(definitions={}, call=call, context=context)
        except NotImplementedError as exc:
            print(f"[演示] 控制流到达第一个未填充检查点 → {exc}")
        print("[说明] 按 docstring 对照表逐个填充 CP1–CP5 即可工作；CP6 已实现")

    asyncio.run(main())

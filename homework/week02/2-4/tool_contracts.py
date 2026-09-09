"""2-4 工具治理：契约层数据模型与接口（只建模，不实现引擎）。

作业要求对照：
1. Python 3.12 + dataclass(frozen=True, slots=True) + Pydantic 2 ✅
2. ToolDefinition：name / description / Pydantic 参数模型 / ToolPolicy /
   handler / precheck / canonical_target ✅
3. ExecutionContext：trace_id / user_id / tenant_id / permissions /
   allowed_tools / mode / approval_id ✅
4. to_model_tool() 只返回名称、描述和 JSON Schema ✅（附边界自检）
5. 给出类型注解；PermissionEngine 与 ToolRuntime 故意不实现 ✅
6. 模型侧与运行侧对象的边界解释见模块尾注与 README。

运行（边界自检）：
    .venv/bin/python tool_contracts.py
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ── 受控词表 ──


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExecutionMode(StrEnum):
    """会话执行模式（可信上下文的一部分，由 Harness 决定，模型不可改）。"""

    SHADOW = "shadow"      # 只记录"本会执行什么"，不真正执行
    DRY_RUN = "dry_run"    # 完整走校验与 precheck，但不产生业务副作用
    ENFORCE = "enforce"    # 正式执行


class PrecheckVerdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


# ── 运行侧：可信上下文 ──


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """服务端可信上下文：由 Harness 从登录态/权限系统/审批系统装配。

    模型输出的任何字段都进不来——它是"能不能执行"的全部判断依据。
    """

    trace_id: str
    user_id: str
    tenant_id: str
    permissions: frozenset[str]        # 该身份已被授予的权限记录
    allowed_tools: frozenset[str]      # 本会话可见/可调用的工具限定名白名单
    mode: ExecutionMode
    approval_id: str | None = None     # 审批系统颁发的单号；未审批为 None


# ── 运行侧：precheck 结果与工具策略 ──


@dataclass(frozen=True, slots=True)
class PrecheckResult:
    """precheck 钩子的裁决（如：订单必须存在、必须已支付）。"""

    verdict: PrecheckVerdict
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """治理策略（工程师配置侧）：声明执行要求，PermissionEngine 据此裁决。"""

    permission: str              # 执行本工具所需的权限，如 "order:read"
    risk: RiskLevel
    timeout_seconds: float
    max_retries: int
    idempotent: bool             # 非幂等写操作：超时不自动重试
    requires_approval: bool = False
    result_max_bytes: int = 32_768   # Tool Result 限长（归一化前的硬上限）


# ── 可调用接口（只签名，不实现） ──

ToolHandler = Callable[[BaseModel, ExecutionContext], Awaitable[BaseModel]]
"""业务 handler：吃（已校验参数，可信上下文），吐（Output Schema 约定的结果）。"""

PrecheckFn = Callable[[BaseModel, ExecutionContext], Awaitable[PrecheckResult]]
"""执行前业务检查：基于参数与上下文裁决，不产生副作用。"""

CanonicalTargetFn = Callable[[BaseModel], str]
"""从已校验参数导出规范化对象标识（如 "order:ord_1001"），
供审批绑定、幂等去重与审计关联使用。"""


# ── 工具契约 ──


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """完整契约：模型侧只看见 to_model_tool() 的投影，其余全部留在服务端。"""

    name: str                          # 限定名，如 "orders.get_order"
    description: str                   # 做什么 / 何时用 / 不做什么
    input_model: type[BaseModel]       # Pydantic 参数模型 = JSON Schema 单一事实来源
    policy: ToolPolicy                 # 治理策略（服务端）
    handler: ToolHandler               # 业务实现（服务端）
    precheck: PrecheckFn | None = None                 # 可选执行前检查
    canonical_target: CanonicalTargetFn | None = None  # 可选规范化目标

    def to_model_tool(self) -> dict[str, Any]:
        """模型侧投影（唯一出口）：只暴露名称、描述和 JSON Schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            },
        }


def assert_model_tool_safe(model_tool: dict[str, Any]) -> None:
    """边界自检：投影里恰好只有公开协议，治理与实现痕迹零出现。"""
    assert set(model_tool) == {"type", "function"}
    function = model_tool["function"]
    assert set(function) == {"name", "description", "parameters"}, (
        f"function 只允许 name/description/parameters，实际有 {sorted(function)}"
    )
    dumped = str(model_tool)
    for forbidden in (
        "handler", "precheck", "canonical_target", "policy",
        "permission", "risk", "timeout", "idempotent",
        "approval", "ExecutionContext", "allowed_tools",
    ):
        assert forbidden not in dumped, f"投影泄露内部字段: {forbidden}"


# ── 示例装配（占位 handler：契约层不实现业务） ──


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GetOrderInput(StrictModel):
    order_id: str = Field(min_length=3, max_length=64)


async def _unimplemented_handler(
    args: BaseModel, ctx: ExecutionContext
) -> BaseModel:
    raise NotImplementedError("契约层只定义接口，业务实现在工具层")


def get_order_definition() -> ToolDefinition:
    return ToolDefinition(
        name="orders.get_order",
        description=(
            "按订单号查询当前用户的订单状态。"
            "用于查看订单详情；只读取订单，不能修改、取消或退款。"
        ),
        input_model=GetOrderInput,
        policy=ToolPolicy(
            permission="order:read",
            risk=RiskLevel.LOW,
            timeout_seconds=5.0,
            max_retries=2,
            idempotent=True,
        ),
        handler=_unimplemented_handler,
        canonical_target=lambda args: f"order:{args.order_id}",
    )


# ── 边界自检 ──

if __name__ == "__main__":
    definition = get_order_definition()
    model_tool = definition.to_model_tool()
    assert_model_tool_safe(model_tool)

    import json

    print(json.dumps(model_tool, ensure_ascii=False, indent=2))
    ctx = ExecutionContext(
        trace_id="trace_42",
        user_id="user_demo",
        tenant_id="tenant_demo",
        permissions=frozenset({"order:read"}),
        allowed_tools=frozenset({"orders.get_order"}),
        mode=ExecutionMode.ENFORCE,
    )
    print("ExecutionContext:", ctx)
    print("canonical_target(order_1001):",
          definition.canonical_target(GetOrderInput(order_id="ord_1001")))
    print("[边界自检通过] 投影只有 name/description/parameters，治理与实现零泄露")

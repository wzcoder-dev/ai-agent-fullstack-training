"""4.2 课堂练习：为 search_orders 完成一次最小工具调用。

模仿瑞幸 AI 开放平台（open.lkcoffee.com）的协议风格：平台面向 Agent 暴露的
是"名称 + 描述 + 参数 Schema"的公开协议（其工具文档以 previewOrder、
getOrderStatus 等工具形式对外提供点单能力），真实业务系统、权限与内部
依赖全部留在服务端，不进入模型可见的协议。

四个步骤：
1. 定义名称、描述和 Input Schema；
2. 生成模型可见的 Tool JSON，断言其中没有 handler、权限和内部依赖；
3. 构造两条不同 tool_call_id 的 Tool Call；
4. 分别生成 Tool Result，断言结果 ID 与原调用一致（含失败路径）。

只使用模拟订单数据；不实现 Registry、Snapshot、完整权限系统或重试框架。
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


# ---------- 步骤 1：名称、描述和 Input Schema ----------


class StrictModel(BaseModel):
    """默认拒绝额外字段、拒绝隐式类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class SearchOrdersInput(StrictModel):
    """模型到 Runtime 的参数协议：约束模型可以填写什么。"""

    status: Literal["pending", "paid", "shipped", "cancelled"] | None = None
    created_from: date | None = None
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def require_filter(self) -> "SearchOrdersInput":
        if self.status is None and self.created_from is None:
            raise ValueError("status 与 created_from 至少提供一个")
        return self


class OrderSummary(StrictModel):
    order_id: str
    status: Literal["pending", "paid", "shipped", "cancelled"]
    created_at: str
    amount_cents: int = Field(ge=0)


class SearchOrdersOutput(StrictModel):
    """handler 到 Tool Result 的成功协议：验收业务函数返回值。"""

    orders: list[OrderSummary]
    total: int = Field(ge=0)


class ToolError(StrictModel):
    """失败到恢复逻辑的协议：code 分类 + retryable 决定下一步。"""

    code: Literal[
        "INVALID_ARGUMENT",
        "TOOL_NOT_FOUND",
        "PERMISSION_DENIED",
        "APPROVAL_REQUIRED",
        "TIMEOUT",
        "UPSTREAM_ERROR",
        "INVALID_OUTPUT",
    ]
    message: str
    retryable: bool = False


class ExecutionContext(StrictModel):
    """可信上下文：来自登录态，由 Harness 注入，模型不可填写。"""

    user_id: str
    tenant_id: str
    trace_id: str


@dataclass(frozen=True)
class ToolDefinition:
    """最小完整契约。

    模型侧只见 name / description / input_model；
    output_model / error_model / permission / risk / handler 留在服务端。
    """

    name: str
    description: str
    input_model: type[SearchOrdersInput]
    output_model: type[SearchOrdersOutput]
    error_model: type[ToolError]
    permission: str
    risk: Literal["low", "medium", "high"]
    handler: Any  # Callable[[SearchOrdersInput, ExecutionContext], Awaitable[...]]


# ---------- 模拟订单数据与业务函数（服务端内部实现） ----------

_ORDERS = [
    {
        "user_id": "user_demo",
        "order_id": "LK202608300001",
        "status": "pending",
        "created_at": "2026-08-30T10:30:00+08:00",
        "amount_cents": 2999,
    },
    {
        "user_id": "user_demo",
        "order_id": "LK202608300002",
        "status": "paid",
        "created_at": "2026-08-30T11:00:00+08:00",
        "amount_cents": 1599,
    },
    {
        "user_id": "user_other",  # 别人的订单：无论模型怎么查都不应出现
        "order_id": "LK202608300003",
        "status": "pending",
        "created_at": "2026-08-30T12:00:00+08:00",
        "amount_cents": 999,
    },
]


async def search_orders(
    args: SearchOrdersInput, ctx: ExecutionContext
) -> SearchOrdersOutput:
    rows = [
        row
        for row in _ORDERS
        if row["user_id"] == ctx.user_id  # 身份来自可信上下文，不是模型参数
        and (args.status is None or row["status"] == args.status)
        and (
            args.created_from is None
            or date.fromisoformat(row["created_at"][:10]) >= args.created_from
        )
    ]
    rows = rows[: args.limit]
    return SearchOrdersOutput(
        # user_id 是内部字段，不进入返回协议
        orders=[
            OrderSummary.model_validate(
                {k: v for k, v in row.items() if k != "user_id"}
            )
            for row in rows
        ],
        total=len(rows),
    )


SEARCH_ORDERS = ToolDefinition(
    name="search_orders",
    description=(
        "按当前用户、订单状态和创建日期查询订单。"
        "用于查看订单列表或状态；只读取订单，"
        "不能取消、退款或修改订单。"
    ),
    input_model=SearchOrdersInput,
    output_model=SearchOrdersOutput,
    error_model=ToolError,
    permission="order:read",
    risk="low",
    handler=search_orders,
)


# ---------- 步骤 2：模型可见的 Tool JSON ----------


def to_model_tool(tool: ToolDefinition) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_model.model_json_schema(),
        },
    }


def assert_model_tool_safe(model_tool: dict) -> None:
    """断言模型可见协议中不含 handler、权限和内部依赖。"""
    function = model_tool["function"]
    assert set(function) == {"name", "description", "parameters"}, (
        f"function 只能包含 name/description/parameters，实际有 {sorted(function)}"
    )
    dumped = json.dumps(model_tool, ensure_ascii=False)
    for forbidden in (
        "handler",
        "permission",
        "risk",
        "timeout",
        "search_orders(",  # 代码引用
        "_ORDERS",  # 内部数据依赖
        "ExecutionContext",
        "user_id",  # 可信上下文字段不得出现在公开协议里
    ):
        assert forbidden not in dumped, f"公开协议泄露了内部字段: {forbidden}"


# ---------- 步骤 3 / 4：Tool Call 与 Tool Result ----------


class ToolCall(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments_json: str


class ToolResultMessage(StrictModel):
    role: Literal["tool"] = "tool"
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


def success_message(call: ToolCall, output: SearchOrdersOutput) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call.id,
        name=call.name,
        content=output.model_dump_json(),
    )


def error_message(call: ToolCall, error: ToolError) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call.id,
        name=call.name,
        content=json.dumps({"error": error.model_dump()}, ensure_ascii=False),
        is_error=True,
    )


async def execute_call(call: ToolCall, ctx: ExecutionContext) -> ToolResultMessage:
    """最小执行路径：校验参数 → 执行 handler → 验收输出；失败转结构化错误。"""
    if call.name != SEARCH_ORDERS.name:
        return error_message(
            call, ToolError(code="TOOL_NOT_FOUND", message=f"未知工具 {call.name}")
        )
    try:
        args = SEARCH_ORDERS.input_model.model_validate_json(call.arguments_json)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        return error_message(
            call,
            ToolError(
                code="INVALID_ARGUMENT",
                message=f"参数校验失败: {detail}",
            ),
        )
    output = await SEARCH_ORDERS.handler(args, ctx)
    validated = SEARCH_ORDERS.output_model.model_validate(output)
    return success_message(call, validated)


# ---------- 演示四步 ----------


async def main() -> None:
    print("=== 步骤 1+2：工具定义与模型可见 JSON ===")
    model_tool = to_model_tool(SEARCH_ORDERS)
    print(json.dumps(model_tool, ensure_ascii=False, indent=2))
    assert_model_tool_safe(model_tool)
    print("[断言通过] 公开协议中没有 handler、权限和内部依赖\n")

    ctx = ExecutionContext(
        user_id="user_demo", tenant_id="tenant_demo", trace_id="trace_42_001"
    )

    print("=== 步骤 3：构造两条不同 tool_call_id 的 Tool Call ===")
    calls = [
        ToolCall(
            id="call_search_001",
            name="search_orders",
            arguments_json=json.dumps({"status": "pending", "limit": 10}),
        ),
        # 第二条走失败路径：status 不在枚举内，应生成 INVALID_ARGUMENT 错误结果
        ToolCall(
            id="call_search_002",
            name="search_orders",
            arguments_json=json.dumps({"status": "refunded", "limit": 5}),
        ),
    ]
    for call in calls:
        print(f"{call.id}: {call.name}({call.arguments_json})")

    print("\n=== 步骤 4：分别生成 Tool Result 并断言 ID 配对 ===")
    for call in calls:
        result = await execute_call(call, ctx)
        print(
            f"-> tool_call_id={result.tool_call_id}, "
            f"is_error={result.is_error}, content={result.content}"
        )
        # 核心验收：无论成功还是失败，结果都保留原始 tool_call_id
        assert result.tool_call_id == call.id, (
            f"结果 ID 与调用 ID 不一致: {result.tool_call_id} != {call.id}"
        )
        assert result.name == call.name
    print("[断言通过] 两条 Tool Result 的 ID 均与原调用一致")


if __name__ == "__main__":
    asyncio.run(main())

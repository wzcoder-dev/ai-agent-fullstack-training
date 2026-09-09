"""4.2 最小工具调用：search_orders（我的版本，引导式完成）。

关卡 1 工具定义（Input Schema）✅
关卡 2 模型可见 Tool JSON + 泄露断言 ✅
关卡 3 两条不同 tool_call_id 的 Tool Call ✅
关卡 4 Output/Error Schema、Tool Result 与 ID 配对断言 ✅
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)


# ── 关卡 1：工具定义 ──


class StrictModel(BaseModel):
    """工具协议基类。

    extra="forbid"：模型多填的字段（哪怕塞进 user_id、approved）直接报校验错误，
    而不是被静默丢弃后流入业务函数。
    strict=True：拒绝隐式类型转换（"10" 不会变 10）；仅 JSON 输入例外——
    JSON 没有日期类型，"2026-08-30" 允许解析成 date。
    """

    model_config = ConfigDict(extra="forbid", strict=True)


class SearchOrdersInput(StrictModel):
    """模型 → Runtime 的参数协议：约束模型可以填什么。"""

    status: Literal["pending", "paid", "shipped", "cancelled"] | None = None
    created_from: date | None = None
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def require_filter(self) -> "SearchOrdersInput":
        # 跨字段约束写在 model_validator：单字段校验够不着"至少一个"这种关系
        if self.status is None and self.created_from is None:
            raise ValueError("status 与 created_from 至少提供一个")
        return self


# ── 服务端内部实现（关卡 4 才执行，这里先就位） ──


class ExecutionContext(StrictModel):
    """可信上下文：来自登录态，由 Harness 注入，模型不可填写。"""

    user_id: str
    tenant_id: str
    trace_id: str


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
) -> "SearchOrdersOutput":
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


# ── 关卡 2：模型可见 Tool JSON ──


@dataclass(frozen=True)
class ToolDefinition:
    """模型侧（name/description/input_model）与 Runtime 侧（permission/risk/handler）分离。"""

    name: str
    description: str
    input_model: type[SearchOrdersInput]
    permission: Literal["order:read", "order:write"]
    risk: Literal["low", "medium", "high"]
    handler: Any


SEARCH_ORDERS = ToolDefinition(
    name="search_orders",
    # 描述写"当前用户"这个语义，不写 user_id 这个实现词
    description=(
        "按当前用户、订单状态和创建日期查询订单。"
        "用于查看订单列表或状态；只读取订单，不能取消、退款或修改订单。"
    ),
    input_model=SearchOrdersInput,
    permission="order:read",
    risk="low",
    handler=search_orders,
)


def to_model_tool(tool: ToolDefinition) -> dict:
    """公开协议。parameters 从 Input Schema 生成：单一事实来源，
    模型可见协议和 Runtime 校验协议永远不会漂移。"""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_model.model_json_schema(),
        },
    }


def assert_model_tool_safe(model_tool: dict) -> None:
    """证明公开协议没有泄露：白名单 + 黑名单，机器可执行。"""
    function = model_tool["function"]
    # 白名单：键必须恰好是这三个，多一个都算泄露
    assert set(function) == {"name", "description", "parameters"}, (
        f"function 只允许 name/description/parameters，实际有 {sorted(function)}"
    )
    # 黑名单：整个协议文本中不得出现内部字段与实现痕迹
    dumped = json.dumps(model_tool, ensure_ascii=False)
    for word in (
        "handler",
        "permission",
        "risk",
        "timeout",
        "user_id",
        "ExecutionContext",
        "order:write",
        "_ORDERS",
    ):
        assert word not in dumped, f"公开协议泄露内部字段: {word}"


# ── 关卡 3：Tool Call（模型输出的"信封"） ──


class ToolCall(StrictModel):
    """只校验信封本身：id 非空、name 非空、arguments 是字符串。

    参数内容对不对，这一层不看——那是 Input Schema 的事；
    arguments_json 保持模型吐出的原始文本，解析权在 Runtime。
    """

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments_json: str


def checkpoint_3() -> None:
    print("── 关卡 3：构造两条不同 tool_call_id 的 Tool Call ──")
    calls = [
        ToolCall(
            id="call_search_001",
            name="search_orders",
            arguments_json=json.dumps({"status": "pending", "limit": 10}),
        ),
        # 构造时完全合法：信封层不看内容；"refunded" 会死在执行层的 Input Schema
        ToolCall(
            id="call_search_002",
            name="search_orders",
            arguments_json=json.dumps({"status": "refunded", "limit": 5}),
        ),
    ]
    for call in calls:
        print(f"{call.id}: {call.name}({call.arguments_json})")
    print("[说明] 第二条构造合法——信封层放行、内容层拦截，分层正是设计所在")


# ── 关卡 4：Output/Error Schema、Tool Result 与 ID 配对 ──


class OrderSummary(StrictModel):
    order_id: str
    status: Literal["pending", "paid", "shipped", "cancelled"]
    created_at: str
    amount_cents: int = Field(ge=0)


class SearchOrdersOutput(StrictModel):
    """handler → Tool Result 的成功协议：下一轮 Loop 可直接消费的稳定事实。"""

    orders: list[OrderSummary]
    total: int = Field(ge=0)


class ToolError(StrictModel):
    """失败协议：code 分类 + retryable，模型据此决定修正、停止还是询问。"""

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


class ToolResultMessage(StrictModel):
    role: Literal["tool"] = "tool"
    tool_call_id: str  # 配对主键：必须带回原调用的 id
    name: str  # 调用名跟随结果走
    content: str  # 结果本体（JSON 文本）
    is_error: bool = False  # 模型区分"事实"与"故障"的开关


def success_message(call: ToolCall, output: SearchOrdersOutput) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call.id, name=call.name, content=output.model_dump_json()
    )


def error_message(call: ToolCall, error: ToolError) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call.id,
        name=call.name,
        content=json.dumps({"error": error.model_dump()}, ensure_ascii=False),
        is_error=True,
    )


async def execute_call(call: ToolCall, ctx: ExecutionContext) -> ToolResultMessage:
    """单次执行链：存在性 → Input Schema → handler → Output Schema → 消息。

    任何一步失败都转成带原始 tool_call_id 的结构化 Tool Result，
    绝不把异常抛给 Agent Loop——失败是给模型的 observation，不是崩溃。
    """
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
                code="INVALID_ARGUMENT", message=f"参数校验失败: {detail}"
            ),
        )
    output = await SEARCH_ORDERS.handler(args, ctx)
    # Output Schema 闸门：handler 违反自己的返回协议属工程故障，
    # 这里若抛 ValidationError 应在真实 Runtime 里转成 INVALID_OUTPUT 记录排查
    validated = SearchOrdersOutput.model_validate(output)
    return success_message(call, validated)


async def checkpoint_4() -> None:
    print("── 关卡 4：Tool Result 与 ID 配对断言 ──")
    ctx = ExecutionContext(
        user_id="user_demo", tenant_id="tenant_demo", trace_id="trace_42_001"
    )
    calls = [
        ToolCall(
            id="call_search_001",
            name="search_orders",
            arguments_json=json.dumps({"status": "pending", "limit": 10}),
        ),
        ToolCall(
            id="call_search_002",
            name="search_orders",
            arguments_json=json.dumps({"status": "refunded", "limit": 5}),
        ),
        # 顺带演示存在性闸门：cancel_order 没有注册给模型
        ToolCall(
            id="call_search_003",
            name="cancel_order",
            arguments_json="{}",
        ),
    ]
    for call in calls:
        result = await execute_call(call, ctx)
        print(
            f"-> tool_call_id={result.tool_call_id}, is_error={result.is_error}, "
            f"content={result.content[:100]}"
        )
        # 核心验收：成功与失败都必须保留原始 tool_call_id
        assert result.tool_call_id == call.id, (
            f"ID 配对断裂: {result.tool_call_id} != {call.id}"
        )
        assert result.name == call.name
    print("[断言通过] 三条结果（成功/参数错误/未知工具）均与原调用 ID 配对")


# ── 冒烟测试 ──


def checkpoint_1() -> None:
    print("── 关卡 1：合法参数 ──")
    print(SearchOrdersInput.model_validate_json('{"status": "pending"}'))
    print(SearchOrdersInput.model_validate_json('{"created_from": "2026-08-30"}'))

    rejections = [
        ("两个过滤条件都缺", "{}"),
        ("status 编造枚举外的值", '{"status": "refunded"}'),
        ("limit 超上限", '{"status": "pending", "limit": 99}'),
        ("多余字段被 forbid 拒绝", '{"status": "pending", "user_id": "attacker"}'),
        ("created_from 不是日期", '{"created_from": "上礼拜"}'),
    ]
    print("── 关卡 1：应当被 Input Schema 拒绝的调用 ──")
    for label, raw in rejections:
        try:
            SearchOrdersInput.model_validate_json(raw)
            print(f"[漏放行!] {label}")
        except Exception as exc:
            first_error = exc.errors()[0]
            print(f"[拒绝] {label} -> {first_error['type']}")


def checkpoint_2() -> None:
    model_tool = to_model_tool(SEARCH_ORDERS)
    print("── 关卡 2：模型可见 Tool JSON ──")
    print(json.dumps(model_tool, ensure_ascii=False, indent=2))
    assert_model_tool_safe(model_tool)
    print("[断言通过] 恰好三个键，敏感词零出现，协议由 Input Schema 生成")


async def main() -> None:
    checkpoint_1()
    print()
    checkpoint_2()
    print()
    checkpoint_3()
    print()
    await checkpoint_4()


if __name__ == "__main__":
    asyncio.run(main())

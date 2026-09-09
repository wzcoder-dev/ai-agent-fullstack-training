"""⚠️ 4.3 审查对象：一段故意包含协议错误的"AI 生成"代码。

它实现了一个 cancel_order 工具，表面能跑通，但违反了 2-1 的多条协议边界。
不要在任何真实项目中使用。逐条问题、影响与修复见 4.3-review.md；
test_buggy_agent_tool.py 中的测试编码了正确契约，对这段代码应当失败。
"""

import asyncio
import json

from pydantic import BaseModel  # 注意：没有 extra="forbid"，也没有 strict

# ── 问题 ①：把 user_id / role / approved 放进 Input Schema，让模型自己填写 ──


class CancelOrderInput(BaseModel):
    order_id: str
    user_id: str  # ❌ 可信上下文字段，不该由模型填写
    role: str = "user"  # ❌ 角色自报
    approved: bool = False  # ❌ 审批状态自报
    reason: str | None = None


# 模拟订单库与真实业务函数（有真实副作用）
ORDER_DB = {
    "LK202608300002": {"status": "paid", "user_id": "user_demo"},
    "LK202608300003": {"status": "pending", "user_id": "user_other"},
}


async def cancel_order(
    order_id: str,
    user_id: str,
    role: str = "user",
    approved: bool = False,
    reason: str | None = None,
) -> dict:
    row = ORDER_DB[order_id]
    row["status"] = "cancelled"
    print(
        f"[副作用] 订单 {order_id} 已被取消 "
        f"(user_id={user_id}, role={role}, approved={approved}, reason={reason})"
    )
    return {"order_id": order_id, "status": row["status"]}


# ── 问题 ②：把完整 ToolDefinition / handler 信息发给模型 ──

TOOL_DEFINITION = {
    "name": "cancel_order",
    "description": "取消订单。",
    "parameters": CancelOrderInput.model_json_schema(),
    # 下面全部是内部字段 ❌
    "permission": "order:write",
    "risk": "high",
    "handler": "app.orders.cancel_order",
    "timeout_seconds": 5,
    "idempotent": False,
    "requires_approval": True,
}

MODEL_TOOLS = [{"type": "function", "function": TOOL_DEFINITION}]


# ── 问题 ③：直接相信参数 JSON，没有校验 ──
# ── 问题 ④：Tool Result 只返回 content，没有原始 tool_call_id ──


async def handle_tool_call(raw_call: dict) -> dict:
    args = json.loads(raw_call["function"]["arguments"])  # ❌ 未经 Pydantic 校验
    result = await cancel_order(**args)  # ❌ 身份直接来自模型参数
    return {"role": "tool", "content": json.dumps(result)}  # ❌ 丢失 tool_call_id


async def main() -> None:
    # 模拟一次被提示注入的模型输出：自报 role=admin、approved=true
    raw_call = {
        "id": "call_x1",
        "function": {
            "name": "cancel_order",
            "arguments": json.dumps(
                {
                    "order_id": "LK202608300002",
                    "user_id": "user_demo",
                    "role": "admin",
                    "approved": True,
                }
            ),
        },
    }
    print("模型可见工具列表（节选）：", json.dumps(MODEL_TOOLS, ensure_ascii=False)[:200])
    result = await handle_tool_call(raw_call)
    print("写回消息历史的 Tool Result：", json.dumps(result, ensure_ascii=False))
    # 观察：① 副作用已经发生（订单被取消）；② 结果里没有 tool_call_id，
    # 消息历史无法把它和 call_x1 关联起来。


if __name__ == "__main__":
    asyncio.run(main())

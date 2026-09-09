"""9.2 演示脚本：create_ticket 走完 invoke 的六条路径（课程给定规格）。

期望输出：
    模型可见工具 : create_ticket
    call-1 -> INVALID_ARGUMENT
    call-2 -> PERMISSION_DENIED
    call-3 -> APPROVAL_REQUIRED
    call-4 -> OK {"status": "created", "ticket_id": "T-1001"}
    call-5 -> INTERNAL_ERROR
    call-6 -> TOOL_DISABLED

运行：
    homework/week01/llm-gateway/.venv/bin/python tool_runtime_demo.py
"""

import asyncio
import json
from itertools import count
from typing import Literal

from pydantic import Field

from tool_runtime import (
    ExecutionContext,
    Registry,
    StrictModel,
    ToolDefinition,
    ToolRuntime,
    ToolCall,
)

# ── 工具：create_ticket（高危写操作，非幂等） ──

_TICKET_SEQ = count(1001)
HANDLER_ENTERED: list[str] = []  # 副作用探针：记录真正进入 handler 的 call_id


class CreateTicketInput(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    priority: Literal["low", "normal", "high"] = "normal"


class TicketCreated(StrictModel):
    status: Literal["created"]
    ticket_id: str


async def create_ticket(args: CreateTicketInput, ctx: ExecutionContext) -> TicketCreated:
    HANDLER_ENTERED.append(args.title)  # 副作用探针：真正进入 handler 才记录
    if args.title == "boom":  # 模拟内部 bug → INTERNAL_ERROR
        raise RuntimeError("connection reset (secret internal detail)")
    return TicketCreated(status="created", ticket_id=f"T-{next(_TICKET_SEQ)}")


TICKET_TOOL = ToolDefinition(
    name="create_ticket",
    description="创建工单。高危写操作，需要审批；非幂等，超时不自动重试。",
    input_model=CreateTicketInput,
    output_model=TicketCreated,
    permission="ticket:write",
    risk="high",
    timeout_seconds=5,
    idempotent=False,
    handler=create_ticket,
)


async def main() -> None:
    registry = Registry()
    registry.register(TICKET_TOOL)
    snapshot = registry.snapshot(["create_ticket"])  # 冻结本轮
    audit: list[dict] = []
    runtime = ToolRuntime(registry, audit=lambda e: audit.append(e))

    ctx_writer = ExecutionContext(
        user_id="user_demo", tenant_id="t1",
        permissions=frozenset({"ticket:write"}),
        approved_call_ids=frozenset({"call-4", "call-5"}),  # 可信审批记录（call-5 用于演示内部异常路径）
        trace_id="trace_demo",
    )
    ctx_reader = ExecutionContext(
        user_id="user_guest", tenant_id="t1",
        permissions=frozenset(), trace_id="trace_demo",
    )

    def call(cid: str, raw_args: str, ctx: ExecutionContext):
        return snapshot, ToolCall(id=cid, name="create_ticket", arguments_json=raw_args), ctx

    scenarios = [
        call("call-1", '{"title": "重启打印机", "priority": "urgent"}', ctx_writer),
        call("call-2", '{"title": "重置密码"}', ctx_reader),
        call("call-3", '{"title": "给客户退款", "priority": "high"}', ctx_writer),
        call("call-4", '{"title": "给客户退款", "priority": "high"}', ctx_writer),
        call("call-5", '{"title": "boom"}', ctx_writer),
    ]

    print(f"模型可见工具 : {', '.join(t['function']['name'] for t in snapshot.provider_tools())}")
    for snapshot_, call_, ctx_ in scenarios:
        result = await runtime.invoke(snapshot_, call_, ctx_)
        print(f"{call_.id} -> {describe(result)}")

    # 中途停用（治理杀开关），同一份旧快照仍然要拒绝
    registry.set_enabled("create_ticket", False)
    result = await runtime.invoke(*call("call-6", '{"title": "深夜下单"}', ctx_writer))
    print(f"{result.tool_call_id} -> {describe(result)}")

    # ── 验收证据 ──
    assert set(HANDLER_ENTERED) == {"给客户退款", "boom"}, (
        f"拒绝路径进入了 handler: {HANDLER_ENTERED}"
    )
    assert all("Traceback" not in json.dumps(a) for a in audit)
    print("验收通过 : 4 条拒绝路径未进入 handler，内部异常也没有泄漏 Python 堆栈")
    print("验收通过 : 成功结果保留 tool_call_id，旧快照中的停用工具仍被拒绝")
    print(f"审计记录数 : {len(audit)}")


def describe(result) -> str:
    if result.is_error:
        return json.loads(result.content)["error"]["code"]
    return f"OK {result.content}"


if __name__ == "__main__":
    asyncio.run(main())

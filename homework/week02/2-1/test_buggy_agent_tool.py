"""4.3 验收测试：编码"正确契约"，对 buggy_agent_tool.py 应当全部失败。

每条测试对应一类协议错误；测试失败本身就是问题的证据。
全部修复后，这些测试应当转绿。

运行：
    homework/week01/llm-gateway/.venv/bin/python -m pytest test_buggy_agent_tool.py -v
"""

import asyncio
import json

import pytest

import buggy_agent_tool as buggy


@pytest.fixture(autouse=True)
def reset_db():
    """每条测试从干净的订单库开始，避免前序测试的副作用互相污染。"""
    buggy.ORDER_DB.clear()
    buggy.ORDER_DB.update(
        {
            "LK202608300002": {"status": "paid", "user_id": "user_demo"},
            "LK202608300003": {"status": "pending", "user_id": "user_other"},
        }
    )


def run(coro):
    return asyncio.run(coro)


def raw_call(order_id="LK202608300002", **overrides) -> dict:
    """模拟一次模型输出的 Tool Call（信封 + JSON 字符串参数）。"""
    args = {
        "order_id": order_id,
        "user_id": "user_demo",
        "role": "user",
        "approved": False,
    }
    args.update(overrides)
    return {
        "id": "call_test_1",
        "function": {"name": "cancel_order", "arguments": json.dumps(args)},
    }


# ── 问题 2：模型可见协议泄露内部字段 → 影响模型选择 ──


def test_public_tool_json_is_minimal():
    function = buggy.MODEL_TOOLS[0]["function"]
    leaked = set(function) - {"name", "description", "parameters"}
    assert not leaked, f"内部字段泄露给模型: {sorted(leaked)}"


# ── 问题 1：可信字段进入 Input Schema → 影响可信边界 ──


def test_input_schema_has_no_trusted_fields():
    properties = buggy.CancelOrderInput.model_json_schema()["properties"]
    trusted = {"user_id", "role", "approved"} & set(properties)
    assert not trusted, f"可信字段进入模型可填参数: {sorted(trusted)}"


# ── 问题 3：参数未校验直通 handler → 影响可信边界 ──


def test_invalid_arguments_never_reach_handler():
    call = raw_call()
    call["function"]["arguments"] = json.dumps({"user_id": "user_demo"})  # 缺 order_id
    before = dict(buggy.ORDER_DB["LK202608300002"])
    result = run(buggy.handle_tool_call(call))
    assert result.get("is_error") is True, "非法参数应转成结构化错误而非异常/成功"
    assert buggy.ORDER_DB["LK202608300002"] == before, "非法参数不应产生业务副作用"


# ── 问题 4：Tool Result 丢失 tool_call_id → 影响结果关联 ──


def test_tool_result_carries_original_call_id():
    call = raw_call()
    result = run(buggy.handle_tool_call(call))
    assert result.get("tool_call_id") == call["id"], (
        f"结果丢失调用 ID，实际字段: {sorted(result)}"
    )


# ── 加分点 A：handler 无越权校验（水平越权） ──


def test_cannot_cancel_other_users_order():
    call = raw_call(order_id="LK202608300003")  # user_other 的订单
    result = run(buggy.handle_tool_call(call))
    assert result.get("is_error") is True, "取消他人订单应被拒绝"
    assert "PERMISSION_DENIED" in result.get("content", "")
    assert buggy.ORDER_DB["LK202608300003"]["status"] == "pending", "越权取消产生了副作用"


# ── 加分点 B：模型自报 approved=true 不能绕过审批 ──


def test_approval_cannot_be_forged_by_model():
    call = raw_call(approved=True, role="admin")  # 模型自报管理员 + 已审批
    result = run(buggy.handle_tool_call(call))
    assert result.get("is_error") is True, "未经可信审批的高危写操作应 APPROVAL_REQUIRED"
    assert "APPROVAL_REQUIRED" in result.get("content", "")
    assert buggy.ORDER_DB["LK202608300002"]["status"] == "paid", "伪造审批产生了副作用"

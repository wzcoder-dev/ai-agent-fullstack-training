"""补充正式题目未逐项覆盖的边界；所有调用均经过 Runtime。"""

import asyncio
from dataclasses import replace

import pytest

import tool_governance_demo as demo


@pytest.fixture(autouse=True)
def restore_accounts():
    snapshot = dict(demo.ACCOUNTS)
    yield
    demo.ACCOUNTS.clear()
    demo.ACCOUNTS.update(snapshot)


def invoke_approved(amount, *, destination="ACC-A-654321", source="ACC-A-123456"):
    runtime, approvals, audit = demo.build_runtime()
    context = demo.base_context(
        permissions=frozenset({"transfer:execute"}), allowed_tools=frozenset({"transfer"}),
    )
    args = {"from_account": source, "to_account": destination, "amount": amount}
    approvals.approve("boundary", context, "transfer", args)
    result = asyncio.run(runtime.invoke(
        demo.ToolCall("boundary_000001", "transfer", args), replace(context, approval_id="boundary"),
    ))
    return result, audit


@pytest.mark.parametrize("amount,code", [
    (40_000.0, "OK"),
    (40_000.01, "TIMEOUT_UNKNOWN"),
    (50_000.01, "EXCEED_LIMIT"),
    (100_000.0, "EXCEED_LIMIT"),
    (-1.0, "INVALID_ARGUMENT"),
    (True, "INVALID_ARGUMENT"),
    ("100", "INVALID_ARGUMENT"),
    (float("nan"), "INVALID_ARGUMENT"),
    (float("inf"), "INVALID_ARGUMENT"),
])
def test_transfer_amount_boundaries(amount, code):
    before = dict(demo.ACCOUNTS)
    result, audit = invoke_approved(amount)
    assert result.code == code
    assert result.retryable is False
    if code == "OK":
        assert demo.ACCOUNTS[("tenant_a", "ACC-A-123456")] == 60_000.0
        assert demo.ACCOUNTS[("tenant_a", "ACC-A-654321")] == 45_000.0
    else:
        assert demo.ACCOUNTS == before
    assert audit.records[-1].code == code


@pytest.mark.parametrize("destination", ["ACC-A-999999", "ACC-B-111111"])
def test_transfer_missing_or_other_tenant_recipient_never_debits(destination):
    before = dict(demo.ACCOUNTS)
    result, audit = invoke_approved(100.0, destination=destination)
    assert result.code == "ACCOUNT_NOT_FOUND"
    assert demo.ACCOUNTS == before
    assert audit.records[-1].phase == "execution"


def test_transfer_other_tenant_source_cannot_fund_payment():
    before = dict(demo.ACCOUNTS)
    result, _ = invoke_approved(100.0, source="ACC-B-111111")
    assert result.code == "INSUFFICIENT_BALANCE"
    assert demo.ACCOUNTS == before


def test_existing_order_email_and_secret_redaction_still_work():
    runtime, _, _ = demo.build_runtime()
    result = asyncio.run(runtime.invoke(
        demo.ToolCall("order_check", "get_order", {"order_id": "ord_1001"}), demo.base_context(),
    ))
    assert result.ok
    assert result.content["customer_email"] == "***@***"
    assert result.content["access_token"] == "***"

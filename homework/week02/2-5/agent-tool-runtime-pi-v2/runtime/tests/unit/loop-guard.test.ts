import { describe, expect, it } from "vitest";
import { LoopGuard } from "../../src/loop-guard.js";

describe("LoopGuard public budget seam", () => {
  it("blocks repeated calls before another side effect can run", () => {
    const guard = new LoopGuard({
      maxTurns: 8,
      maxToolCalls: 8,
      maxRepeatedCall: 1,
      maxTotalTokens: 10_000,
      maxCostUsd: 1,
      maxDurationMs: 60_000,
    });

    expect(guard.beforeToolCall("ticket__create", { order_id: "ORD-A-1001" }).allow).toBe(true);
    expect(guard.beforeToolCall("ticket__create", { order_id: "ORD-A-1001" })).toMatchObject({
      allow: false,
      code: "REPEATED_TOOL_CALL",
    });
  });

  it("stops before a new turn when token, cost, turn, time, or approval budget is exhausted", () => {
    const guard = new LoopGuard({
      maxTurns: 1,
      maxToolCalls: 8,
      maxRepeatedCall: 2,
      maxTotalTokens: 100,
      maxCostUsd: 0.01,
      maxDurationMs: 60_000,
    });
    guard.observeAssistantUsage({ totalTokens: 101, costUsd: 0.001 });
    guard.observeTurn();

    expect(guard.shouldStop()).toMatchObject({ stop: true, code: "MAX_TURNS" });

    const approvalGuard = new LoopGuard({
      maxTurns: 8,
      maxToolCalls: 8,
      maxRepeatedCall: 2,
      maxTotalTokens: 10_000,
      maxCostUsd: 1,
      maxDurationMs: 60_000,
    });
    expect(approvalGuard.shouldStop("approval-1")).toEqual({
      stop: true,
      code: "AWAITING_APPROVAL",
      reason: "工具调用正在等待人工审批",
    });
  });
});

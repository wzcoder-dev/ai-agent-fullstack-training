import { randomUUID } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterAll, afterEach, describe, expect, it } from "vitest";
import type { ExecutionContext } from "../../src/domain.js";
import type { InvocationResult } from "../../src/runtime.js";
import { buildPlatform, executionContext, type Platform } from "../../src/platform.js";
import { LoopGuard } from "../../src/loop-guard.js";
import type { TransferPayload } from "../../src/transfer/service.js";
import { DEMO_ACCOUNTS } from "../../src/transfer/service.js";

const CONFIG = resolve(process.cwd(), "../config/platform.yaml");
const platforms: Platform[] = [];

async function createHarness(): Promise<Platform> {
  const directory = mkdtempSync(join(tmpdir(), "pi-transfer-test-"));
  const platform = await buildPlatform({
    configPath: CONFIG,
    connectMcp: false,
    databaseOverride: join(directory, "platform.db"),
  });
  platform.transfers.seedDemoAccounts(true);
  platforms.push(platform);
  return platform;
}

afterAll(async () => {
  for (const platform of platforms) await platform.close();
});

const FAULT_BACKUP = process.env.TRANSFER_FAULT;
afterEach(() => {
  if (FAULT_BACKUP === undefined) delete process.env.TRANSFER_FAULT;
  else process.env.TRANSFER_FAULT = FAULT_BACKUP;
});

function ctx(overrides: Partial<Parameters<typeof executionContext>[0]> = {}): ExecutionContext {
  return executionContext({
    traceId: randomUUID(),
    requestId: randomUUID(),
    userId: "trainer-1",
    tenantId: "tenant_a",
    roles: ["supervisor"],
    ...overrides,
  });
}

function args(overrides: Record<string, unknown> = {}) {
  return { from_account: "A", to_account: "B", amount_minor: 10_000, currency: "CNY", ...overrides };
}

type SubmitOptions = { key?: string; context?: ExecutionContext };

async function submit(
  platform: Platform,
  argsValue: Record<string, unknown>,
  keyOrOptions?: string | SubmitOptions,
): Promise<InvocationResult> {
  const options: SubmitOptions = typeof keyOrOptions === "string" ? { key: keyOrOptions } : (keyOrOptions ?? {});
  const context = options.context ?? ctx();
  return platform.runtime.invoke({
    context,
    toolCallId: randomUUID(),
    modelName: "account__transfer",
    args: argsValue,
    idempotencyKey: options.key ?? `test:${context.traceId}`,
  });
}

/** 提交并取回请求 ID（假定结果带 transfer_request_id）。 */
async function submitForId(
  platform: Platform,
  argsValue: Record<string, unknown>,
  keyOrOptions?: string | SubmitOptions,
): Promise<string> {
  const result = await submit(platform, argsValue, keyOrOptions);
  const payload = result.content as TransferPayload;
  expect(payload.transfer_request_id).toBeTruthy();
  return payload.transfer_request_id!;
}

async function authAndResume(platform: Platform, requestId: string, password = "123456") {
  return platform.transfers.authenticateAndResume(requestId, password, ctx());
}

function payloadOf(result: InvocationResult | { payload: TransferPayload }): TransferPayload {
  const maybe = result as { payload?: TransferPayload; content?: unknown };
  return (maybe.payload ?? maybe.content) as TransferPayload;
}

function balanceOf(platform: Platform, accountId: string): number {
  return platform.transfers.store.getAccount(accountId)!.balance_minor;
}

describe("模拟转账：正常链路", () => {
  it("正常账户转账：认证成功后余额正确变化且只有一条流水", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args(), "normal-1");

    const waiting = await submit(platform, args(), { key: "normal-1" });
    expect(payloadOf(waiting).status).toBe("WAITING_AUTH"); // 认证前的重复提交只返回当前状态

    const result = await authAndResume(platform, requestId);
    const payload = payloadOf(result as InvocationResult);
    expect(payload.status).toBe("SUCCEEDED");
    expect(payload.transaction_id).toMatch(/^TX-/);
    expect(payload.from_account).toBe("A***");
    expect(payload.to_account).toBe("B***");
    expect(payload.amount_minor).toBe(10_000);
    expect(payload.retryable).not.toBe(true);

    expect(balanceOf(platform, "A")).toBe(1_000_000 - 10_000);
    expect(balanceOf(platform, "B")).toBe(10_000);
    expect(platform.transfers.store.countTransactions()).toBe(1);
    expect(platform.transfers.store.getRequest(requestId)!.status).toBe("SUCCEEDED");
  });

  it("认证记录被一次性消费：同 key 再次恢复命中幂等而非二次扣款", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args(), "consume-1");
    await authAndResume(platform, requestId);
    const again = await authAndResume(platform, requestId);
    const payload = payloadOf(again as InvocationResult);
    expect(payload.status).toBe("SUCCEEDED");
    expect(platform.transfers.store.countTransactions()).toBe(1);
    expect(balanceOf(platform, "A")).toBe(990_000);
  });
});

describe("模拟转账：Schema 与 Precheck", () => {
  it("零金额、负数返回 INVALID_AMOUNT，且不进入资金执行", async () => {
    const platform = await createHarness();
    for (const amount of [0, -1]) {
      const result = await submit(platform, args({ amount_minor: amount }), `invalid-amount-${amount}`);
      expect(result).toMatchObject({ ok: false, code: "INVALID_AMOUNT" });
      expect(payloadOf(result).retryable).toBe(false);
    }
    expect(platform.transfers.store.countTransactions()).toBe(0);
    expect(balanceOf(platform, "A")).toBe(1_000_000);
  });

  it("非整数、超出安全整数、空白账户、非法币种、额外参数被 Schema 拒绝", async () => {
    const platform = await createHarness();
    const bad: Array<Record<string, unknown>> = [
      args({ amount_minor: 100.5 }),
      args({ amount_minor: 2 ** 53 }),
      args({ from_account: "   " }),
      args({ currency: "USD" }),
      args({ extra_field: "x" }),
      args({ verified: true, user_id: "trainer-2" }),
    ];
    for (const [index, badArgs] of bad.entries()) {
      const result = await submit(platform, badArgs, `schema-${index}`);
      expect(result.code, JSON.stringify(badArgs)).toBe("INVALID_ARGUMENT");
    }
    expect(platform.transfers.store.listRequests()).toHaveLength(0);
  });

  it("余额不足返回 INSUFFICIENT_BALANCE，无资金副作用", async () => {
    const platform = await createHarness();
    platform.transfers.store.setAccountBalance("A", 5_000); // 余额低于转账金额（金额本身不超单笔上限）
    const result = await submit(platform, args({ amount_minor: 100_000 }), "insufficient-1");
    expect(result).toMatchObject({ ok: false, code: "INSUFFICIENT_BALANCE" });
    expect(platform.transfers.store.countTransactions()).toBe(0);
    expect(balanceOf(platform, "A")).toBe(5_000);
  });

  it("转出与收款账户相同返回 SAME_ACCOUNT；账户不存在返回 SOURCE_ACCOUNT_NOT_FOUND", async () => {
    const platform = await createHarness();
    const same = await submit(platform, args({ to_account: "A" }), "same-1");
    expect(same.code).toBe("SAME_ACCOUNT");
    const missing = await submit(platform, args({ from_account: "NOPE" }), "missing-1");
    expect(missing.code).toBe("SOURCE_ACCOUNT_NOT_FOUND");
  });
});

describe("模拟转账：权限与策略", () => {
  it("越权转出（他人账户）与跨租户收款被拒绝", async () => {
    const platform = await createHarness();
    const foreign = await submit(platform, args({ from_account: "FOREIGN" }), "foreign-1");
    expect(foreign).toMatchObject({ ok: false, code: "ACCOUNT_ACCESS_DENIED" });
    const crossTenant = await submit(platform, args({ to_account: "CBANK" }), "cross-1");
    expect(crossTenant).toMatchObject({ ok: false, code: "CROSS_TENANT_DENIED" });
    expect(platform.transfers.store.countTransactions()).toBe(0);
  });

  it("Agent 白名单、角色权限与 plan 模式限制生效（不进入 handler）", async () => {
    const platform = await createHarness();
    const notVisible = await submit(platform, args(), { context: ctx({ agentName: "intern-agent" }) });
    expect(notVisible).toMatchObject({ ok: false, code: "TOOL_NOT_VISIBLE" });
    const noRole = await submit(platform, args(), { context: ctx({ roles: ["support"] }) });
    expect(noRole).toMatchObject({ ok: false, code: "RBAC_DENIED" });
    const planMode = await submit(platform, args(), { context: ctx({ mode: "plan" }) });
    expect(planMode).toMatchObject({ ok: false, code: "PLAN_MODE_DENIED" });
    expect(platform.transfers.store.listRequests()).toHaveLength(0);
  });

  it("单笔上限与每日限额生效（每日限额按转出账户、UTC 日统计）", async () => {
    const platform = await createHarness();
    platform.transfers.store.setAccountBalance("A", 2_000_000);

    const single = await submit(platform, args({ amount_minor: 500_001 }), "limit-single");
    expect(single).toMatchObject({ ok: false, code: "SINGLE_LIMIT_EXCEEDED" });

    // 每笔 400000 分（≤ 单笔上限 500000），三笔累计 1200000 > 每日上限 1000000。
    const first = await submitForId(platform, args({ amount_minor: 400_000 }), "limit-daily-1");
    await authAndResume(platform, first);
    expect(payloadOf(await platform.transfers.statusQuery("limit-daily-1", ctx())).status).toBe("SUCCEEDED");
    const second = await submitForId(platform, args({ amount_minor: 400_000 }), "limit-daily-2");
    await authAndResume(platform, second);
    expect(payloadOf(await platform.transfers.statusQuery("limit-daily-2", ctx())).status).toBe("SUCCEEDED");

    const third = await submit(platform, args({ amount_minor: 400_000 }), "limit-daily-3");
    expect(third).toMatchObject({ ok: false, code: "DAILY_LIMIT_EXCEEDED" });
    expect(balanceOf(platform, "A")).toBe(2_000_000 - 800_000);
  });
});

describe("模拟转账：审批与修改", () => {
  it("风险账户进入 WAITING_APPROVAL，批准后仍必须认证", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args({ to_account: "R" }), "risky-1");
    expect(platform.transfers.store.getRequest(requestId)!.status).toBe("WAITING_APPROVAL");

    const approved = await platform.transfers.approveTransfer(requestId, "teacher", ctx());
    expect(payloadOf(approved)).toMatchObject({ status: "WAITING_AUTH", required_action: "authenticate" });
    // 批准本身不能产生任何资金移动。
    expect(balanceOf(platform, "R")).toBe(0);
    expect(platform.transfers.store.countTransactions()).toBe(0);

    const done = await authAndResume(platform, requestId);
    expect(payloadOf(done as InvocationResult).status).toBe("SUCCEEDED");
    expect(balanceOf(platform, "R")).toBe(10_000);
  });

  it("审批拒绝后无法执行且无资金副作用", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args({ to_account: "R" }), "reject-1");
    await platform.transfers.rejectTransfer(requestId, "teacher", ctx());
    expect(platform.transfers.store.getRequest(requestId)!.status).toBe("REJECTED");

    const resume = await authAndResume(platform, requestId);
    expect(payloadOf(resume)).toMatchObject({ status: "REJECTED", error_code: "APPROVAL_REJECTED" });
    expect(platform.transfers.store.countTransactions()).toBe(0);
    expect(balanceOf(platform, "A")).toBe(1_000_000);
  });

  it("收款账户不存在只能修改，不能通过 approve 跳过检查", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args({ to_account: "X" }), "missing-dest-1");
    expect(platform.transfers.store.getRequest(requestId)!.status).toBe("WAITING_MODIFICATION");

    const approveAttempt = await platform.transfers.approveTransfer(requestId, "teacher", ctx());
    expect(approveAttempt.code).toBe("REQUEST_NOT_EXECUTABLE");

    const modified = (await platform.transfers.modifyTransfer(
      requestId,
      { to_account: "B" },
      "teacher",
      ctx(),
    )) as InvocationResult;
    expect(payloadOf(modified)).toMatchObject({ status: "WAITING_AUTH" });
    const newId = payloadOf(modified).transfer_request_id!;
    expect(newId).not.toBe(requestId);
    expect(platform.transfers.store.getRequest(requestId)).toMatchObject({ status: "SUPERSEDED", superseded_by: newId });
  });

  it("修改生成新请求：旧审批、旧认证不可复用，风险账户不因旧获批而自动批准", async () => {
    const platform = await createHarness();
    // 风险账户请求获批后等待认证；人工改为另一个风险账户 R2 时必须重新审批。
    const riskyRequestId = await submitForId(platform, args({ to_account: "R" }), "modify-risky-1");
    await platform.transfers.approveTransfer(riskyRequestId, "teacher", ctx());
    // 预置一个有效认证记录（等待认证期间已完成认证），修改后必须失效。
    platform.transfers.store.insertAuthRecord({
      requestId: riskyRequestId,
      userId: "trainer-1",
      tenantId: "tenant_a",
      argsDigest: platform.transfers.store.getRequest(riskyRequestId)!.args_digest,
      ttlSeconds: 300,
    });

    const modified = (await platform.transfers.modifyTransfer(
      riskyRequestId,
      { to_account: "R", amount_minor: 20_000 },
      "teacher",
      ctx(),
    )) as InvocationResult;
    const newPayload = payloadOf(modified);
    expect(newPayload.status).toBe("WAITING_APPROVAL"); // 新风险请求不自动获批
    const newId = newPayload.transfer_request_id!;

    // 旧请求终态，旧认证不能让其复活。
    const oldResume = await authAndResume(platform, riskyRequestId);
    expect(payloadOf(oldResume)).toMatchObject({ status: "SUPERSEDED", error_code: "REQUEST_NOT_EXECUTABLE" });

    // 旧审批（被修改时已失效）不能再驱动任何执行。
    const oldApprovalId = platform.transfers.store.getRequest(riskyRequestId)!.approval_id!;
    const reApprove = await platform.transfers.approveTransfer(oldApprovalId, "teacher", ctx());
    expect(reApprove.code).toBe("REQUEST_NOT_EXECUTABLE");

    // 新请求走完整链路：批准 + 认证后成功。
    await platform.transfers.approveTransfer(newId, "teacher", ctx());
    const done = await authAndResume(platform, newId);
    expect(payloadOf(done as InvocationResult).status).toBe("SUCCEEDED");
    expect(balanceOf(platform, "R")).toBe(20_000);
    expect(platform.transfers.store.countTransactions()).toBe(1);
  });
});

describe("模拟转账：支付认证", () => {
  it("密码错误返回 AUTH_FAILED，请求终止", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args(), "auth-fail-1");
    const result = await authAndResume(platform, requestId, "000000");
    expect(payloadOf(result)).toMatchObject({ status: "AUTH_FAILED", error_code: "AUTH_FAILED" });
    expect(platform.transfers.store.getRequest(requestId)!.status).toBe("AUTH_FAILED");
    expect(platform.transfers.store.countTransactions()).toBe(0);

    // 终态后即使密码正确也不能执行。
    const retry = await authAndResume(platform, requestId, "123456");
    expect(payloadOf(retry)).toMatchObject({ status: "AUTH_FAILED" });
    expect(balanceOf(platform, "A")).toBe(1_000_000);
  });

  it("认证过期返回 AUTH_EXPIRED，不执行且可重新认证", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args(), "auth-expire-1");
    platform.transfers.store.insertAuthRecord({
      requestId,
      userId: "trainer-1",
      tenantId: "tenant_a",
      argsDigest: platform.transfers.store.getRequest(requestId)!.args_digest,
      ttlSeconds: -1, // 立即过期
    });
    const expired = await submit(platform, args(), { key: "auth-expire-1" });
    expect(expired.code).toBe("AUTH_EXPIRED");
    expect(platform.transfers.store.getRequest(requestId)!.status).toBe("WAITING_AUTH");
    expect(platform.transfers.store.countTransactions()).toBe(0);

    const fresh = await authAndResume(platform, requestId);
    expect(payloadOf(fresh as InvocationResult).status).toBe("SUCCEEDED");
  });

  it("没有有效认证时只返回结构化等待结果，不阻塞也不执行", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args(), "no-auth-1");
    const result = await submit(platform, args(), { key: "no-auth-1" });
    expect(result.ok).toBe(true);
    expect(payloadOf(result)).toMatchObject({ status: "WAITING_AUTH", required_action: "authenticate" });
    expect(platform.transfers.store.countTransactions()).toBe(0);
    expect(platform.transfers.store.getRequest(requestId)!.status).toBe("WAITING_AUTH");
  });
});

describe("模拟转账：恢复时重做校验", () => {
  it("等待期间余额减少：事务内拒绝并回滚", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args({ amount_minor: 500_000 }), "recheck-balance-1");
    platform.transfers.store.setAccountBalance("A", 100_000); // Precheck 之后余额变化
    const result = await authAndResume(platform, requestId);
    expect(payloadOf(result as InvocationResult)).toMatchObject({
      status: "PRECHECK_FAILED",
      error_code: "INSUFFICIENT_BALANCE",
    });
    expect(balanceOf(platform, "A")).toBe(100_000);
    expect(balanceOf(platform, "B")).toBe(0);
    expect(platform.transfers.store.countTransactions()).toBe(0);
  });

  it("等待期间收款账户升级为风险：重新进入审批而非直接执行", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args(), "recheck-risk-1");
    platform.transfers.store.setAccountRisk("B", "RISKY");
    const result = await authAndResume(platform, requestId);
    const payload = payloadOf(result as InvocationResult);
    expect(payload.error_code).toBe("RISK_ESCALATED");
    expect(payload.status).toBe("WAITING_APPROVAL");
    const request = platform.transfers.store.getRequest(requestId)!;
    expect(request.status).toBe("WAITING_APPROVAL");
    expect(request.approval_id).toBeTruthy(); // 重新创建了审批
    expect(platform.transfers.store.countTransactions()).toBe(0);

    // 新审批 + 重新认证后才能完成。
    await platform.transfers.approveTransfer(requestId, "teacher", ctx());
    const done = await authAndResume(platform, requestId);
    expect(payloadOf(done as InvocationResult).status).toBe("SUCCEEDED");
  });
});

describe("模拟转账：幂等与请求身份", () => {
  it("同 key、同参数重复提交只扣款一次", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args(), "idem-1");
    await authAndResume(platform, requestId);
    const duplicate = await submit(platform, args(), { key: "idem-1" });
    const payload = payloadOf(duplicate);
    expect(payload.status).toBe("SUCCEEDED");
    expect(payload.message).toContain("幂等");
    expect(platform.transfers.store.countTransactions()).toBe(1);
    expect(balanceOf(platform, "A")).toBe(990_000);
  });

  it("同 key、不同参数返回 IDEMPOTENCY_CONFLICT", async () => {
    const platform = await createHarness();
    await submitForId(platform, args(), "idem-conflict-1");
    const conflict = await submit(platform, args({ amount_minor: 20_000 }), { key: "idem-conflict-1" });
    expect(conflict).toMatchObject({ ok: false, code: "IDEMPOTENCY_CONFLICT" });
    expect(platform.transfers.store.countTransactions()).toBe(0);
  });

  it("终态失败请求不能因重复提交自动重新执行", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args({ to_account: "R" }), "terminal-1");
    await platform.transfers.rejectTransfer(requestId, "teacher", ctx());
    const repeat = await submit(platform, args({ to_account: "R" }), { key: "terminal-1" });
    expect(repeat.code).toBe("APPROVAL_REJECTED");
    expect(platform.transfers.store.countTransactions()).toBe(0);
  });

  it("历史查询校验属主：其他用户/租户不能仅凭 key 读取交易", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args(), "access-1");
    await authAndResume(platform, requestId);
    const ok = platform.transfers.statusQuery("access-1", ctx());
    expect(ok.ok).toBe(true);
    const stranger = platform.transfers.statusQuery("access-1", ctx({ userId: "trainer-2" }));
    expect(stranger.code).toBe("REQUEST_NOT_FOUND");
    const otherTenant = platform.transfers.statusQuery("access-1", ctx({ tenantId: "tenant_b" }));
    expect(otherTenant.code).toBe("REQUEST_NOT_FOUND");
  });
});

describe("模拟转账：故障注入与结果未知", () => {
  it("扣款后、提交前异常：余额与流水全部回滚", async () => {
    const platform = await createHarness();
    process.env.TRANSFER_FAULT = "after_debit";
    const requestId = await submitForId(platform, args(), "fault-rollback-1");
    const result = await authAndResume(platform, requestId);
    expect(payloadOf(result as InvocationResult)).toMatchObject({
      status: "EXECUTION_FAILED",
      error_code: "EXECUTION_FAILED",
    });
    expect(balanceOf(platform, "A")).toBe(1_000_000);
    expect(balanceOf(platform, "B")).toBe(0);
    expect(platform.transfers.store.countTransactions()).toBe(0);
    expect(platform.transfers.store.getRequest(requestId)!.status).toBe("EXECUTION_FAILED");
  });

  it("COMMIT 后响应丢失：调用方 OUTCOME_UNKNOWN，按 key 查询得到成功且不重复扣款", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args(), "fault-drop-1");
    process.env.TRANSFER_FAULT = "post_commit_drop";
    const result = await authAndResume(platform, requestId);
    const payload = payloadOf(result as InvocationResult);
    expect(result.code).toBe("OUTCOME_UNKNOWN");
    expect(payload.status).toBe("OUTCOME_UNKNOWN"); // 调用方观察状态 ≠ 数据库事实
    expect(payload.retryable).toBe(false);

    const queried = platform.transfers.statusQuery("fault-drop-1", ctx());
    expect(payloadOf(queried)).toMatchObject({ status: "SUCCEEDED" });

    const duplicate = await submit(platform, args(), { key: "fault-drop-1" });
    expect(payloadOf(duplicate).status).toBe("SUCCEEDED");
    expect(platform.transfers.store.countTransactions()).toBe(1);
    expect(balanceOf(platform, "A")).toBe(990_000);
  });

  it("提交后挂起导致调用超时：数据库仍成功，通过查询确认", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args(), "fault-hang-1");
    process.env.TRANSFER_FAULT = "post_commit_hang";
    const result = await authAndResume(platform, requestId);
    // 工具超时（timeoutMs=1000 < 挂起 1500ms）调用方得到 TOOL_TIMEOUT，结果未知；
    // 但不能据此认为“钱没转出去”。
    expect(result.code).toBe("TOOL_TIMEOUT");
    const queried = platform.transfers.statusQuery(requestId, ctx());
    expect(payloadOf(queried).status).toBe("SUCCEEDED");
    expect(balanceOf(platform, "B")).toBe(10_000);
    expect(platform.transfers.store.countTransactions()).toBe(1);
  }, 20_000);
});

describe("模拟转账：Agent Loop 停止行为", () => {
  it("同一 Run 内第二次转账调用被确定性拦截（无论等待、成功或失败）", async () => {
    const platform = await createHarness();
    const context = ctx();
    const first = await platform.runtime.invoke({
      context,
      toolCallId: randomUUID(),
      modelName: "account__transfer",
      args: args(),
      idempotencyKey: `agent:${context.traceId}:1`,
    });
    expect(first.ok).toBe(true);
    expect(platform.transfers.hasBlockingTransfer(context.traceId)).toBe(true);

    // 模型换 toolCallId（即换派生幂等键）重试同一转账：服务端拦截。
    const second = await platform.runtime.invoke({
      context,
      toolCallId: randomUUID(),
      modelName: "account__transfer",
      args: args({ amount_minor: 20_000 }),
      idempotencyKey: `agent:${context.traceId}:2`,
    });
    expect(second.code).toBe("TRANSFER_ALREADY_REQUESTED");
    expect(platform.transfers.store.countRequestsByTrace(context.traceId)).toBe(1);
  });

  it("失败后的同 Run 重试同样被拦截；LoopGuard 在等待转账时停止", async () => {
    const platform = await createHarness();
    const context = ctx();
    await platform.runtime.invoke({
      context,
      toolCallId: randomUUID(),
      modelName: "account__transfer",
      args: args({ amount_minor: 0 }),
      idempotencyKey: `agent:${context.traceId}:1`,
    });
    const retry = await platform.runtime.invoke({
      context,
      toolCallId: randomUUID(),
      modelName: "account__transfer",
      args: args({ amount_minor: 10_000 }),
      idempotencyKey: `agent:${context.traceId}:2`,
    });
    expect(retry.code).toBe("TRANSFER_ALREADY_REQUESTED");

    const guard = new LoopGuard({
      maxTurns: 8,
      maxToolCalls: 12,
      maxRepeatedCall: 2,
      maxTotalTokens: 30_000,
      maxCostUsd: 0.25,
      maxDurationMs: 120_000,
    });
    expect(guard.shouldStop(undefined, true)).toMatchObject({ stop: true, code: "TRANSFER_WAITING" });
    expect(guard.shouldStop("approval-1", false)).toMatchObject({ stop: true, code: "AWAITING_APPROVAL" });
  });
});

describe("模拟转账：Trace、Audit 与脱敏", () => {
  it("失败、等待与成功路径都有记录，可按 trace/请求关联", async () => {
    const platform = await createHarness();
    const context = ctx();
    const submitted = await platform.runtime.invoke({
      context,
      toolCallId: randomUUID(),
      modelName: "account__transfer",
      args: args(),
      idempotencyKey: `trace:${context.traceId}`,
    });
    const requestId = (submitted.content as TransferPayload).transfer_request_id!;
    expect(requestId).toBeTruthy();
    await platform.transfers.authenticateAndResume(requestId, "123456", ctx());

    const traceEvents = platform.store.getTrace(context.traceId);
    const types = traceEvents.map((event) => event.event_type);
    expect(types).toContain("transfer.request.created");
    expect(types).toContain("transfer.waiting");
    // 等待路径也有记录，且事件与请求关联。
    expect(traceEvents.every((event) => event.trace_id === context.traceId)).toBe(true);

    // 审计包含请求 ID，且全库任何事件都不含明文密码。
    const auditAll = platform.store.listAudit() as Array<Record<string, unknown>>;
    expect(auditAll.some((row) => JSON.stringify(row).includes(requestId))).toBe(true);
    const everything = JSON.stringify({
      audit: auditAll,
      trace: traceEvents,
    });
    expect(everything).not.toContain("123456");
    // 账户号不出现原文（只允许部分脱敏形式）。
    expect(everything).not.toMatch(/"from_account":\s*"A"/);
    expect(everything).not.toMatch(/"to_account":\s*"B"/);
  });

  it("观测写入失败不会把已提交的交易改写成回滚失败", async () => {
    const platform = await createHarness();
    const requestId = await submitForId(platform, args(), "obs-1");
    const originalAddTrace = platform.store.addTrace.bind(platform.store);
    platform.store.addTrace = (() => {
      throw new Error("observability down");
    }) as typeof platform.store.addTrace;
    try {
      const result = await authAndResume(platform, requestId);
      const payload = payloadOf(result as InvocationResult);
      expect(payload.status).toBe("SUCCEEDED");
      expect(payload.observability_degraded).toBe(true);
      // 数据库事实不受观测故障影响。
      expect(platform.transfers.store.getRequest(requestId)!.status).toBe("SUCCEEDED");
      expect(balanceOf(platform, "B")).toBe(10_000);
      expect(platform.transfers.store.countTransactions()).toBe(1);
    } finally {
      platform.store.addTrace = originalAddTrace;
    }
  });
});

describe("模拟转账：演示数据", () => {
  it("默认初始化不覆盖已有数据，force 才显式重置", async () => {
    const platform = await createHarness();
    platform.transfers.store.setAccountBalance("A", 555_555);
    const untouched = platform.transfers.seedDemoAccounts(false);
    expect(untouched.seeded).toBe(false);
    expect(balanceOf(platform, "A")).toBe(555_555);

    const reset = platform.transfers.seedDemoAccounts(true);
    expect(reset.seeded).toBe(true);
    expect(balanceOf(platform, "A")).toBe(DEMO_ACCOUNTS[0].balance_minor);
    expect(platform.transfers.store.countTransactions()).toBe(0);
  });
});

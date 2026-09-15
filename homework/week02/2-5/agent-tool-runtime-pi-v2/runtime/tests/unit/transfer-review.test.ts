import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { buildPlatform, executionContext, type Platform } from "../../src/platform.js";
import type { TransferPayload } from "../../src/transfer/service.js";
import * as platformModule from "../../src/platform.js";
import managedExtension from "../../src/pi/coding-agent-extension.js";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Independent acceptance tests: assert the requested contract, including currently failing cases.
const resources: Array<{ platform: Platform; dir: string }> = [];
const context = (overrides: Record<string, unknown> = {}) => executionContext({
  traceId: randomUUID(), requestId: randomUUID(), userId: "trainer-1",
  tenantId: "tenant_a", roles: ["supervisor"], ...overrides,
});
const args = (overrides: Record<string, unknown> = {}) => ({
  from_account: "A", to_account: "B", amount_minor: 10000, currency: "CNY", ...overrides,
});
async function harness() {
  const dir = mkdtempSync(join(tmpdir(), "transfer-review-"));
  const platform = await buildPlatform({ configPath: resolve("../config/platform.yaml"), connectMcp: false, databaseOverride: join(dir, "test.db") });
  platform.transfers.seedDemoAccounts();
  resources.push({ platform, dir });
  return platform;
}
async function submit(p: Platform, values = args(), ctx = context(), key: string = randomUUID()) {
  return p.runtime.invoke({ context: ctx, toolCallId: randomUUID(), modelName: "account__transfer", args: values, idempotencyKey: key });
}
function payload(result: any): TransferPayload { return result.payload ?? result.content; }
async function request(p: Platform, values = args()) {
  const result = await submit(p, values);
  expect(payload(result).transfer_request_id).toBeTruthy();
  return payload(result).transfer_request_id!;
}
function noMoneyMoved(p: Platform) {
  expect(p.transfers.store.countTransactions()).toBe(0);
  expect(p.transfers.store.getAccount("A")!.balance_minor).toBe(1000000);
}
afterEach(async () => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  for (const { platform, dir } of resources.splice(0)) {
    await platform.close();
    platform.store.database.close();
    rmSync(dir, { recursive: true, force: true });
  }
});

describe("review: normal and numeric boundaries", () => {
  it.each([1, 10000, 500000])("transfers exactly %i minor units", async amount => {
    const p = await harness();
    const id = await request(p, args({ amount_minor: amount }));
    const result = await p.transfers.authenticateAndResume(id, "123456", context());
    expect(payload(result).status).toBe("SUCCEEDED");
    expect(p.transfers.store.getAccount("A")!.balance_minor).toBe(1000000 - amount);
    expect(p.transfers.store.getAccount("B")!.balance_minor).toBe(amount);
    expect(p.transfers.store.countTransactions()).toBe(1);
  });
  it("allows the exact daily limit and rejects one additional cent", async () => {
    const p = await harness();
    p.transfers.store.setAccountBalance("A", 2000000);
    for (let n = 0; n < 2; n++) {
      const id = await request(p, args({ amount_minor: 500000 }));
      expect(payload(await p.transfers.authenticateAndResume(id, "123456", context())).status).toBe("SUCCEEDED");
    }
    expect((await submit(p, args({ amount_minor: 1 }))).code).toBe("DAILY_LIMIT_EXCEEDED");
    expect(p.transfers.store.countTransactions()).toBe(2);
  });
  it.each([true, "10000"])("rejects non-number amount %j instead of coercing it", async amount => {
    const p = await harness();
    expect((await submit(p, args({ amount_minor: amount }))).code).toBe("INVALID_ARGUMENT");
    noMoneyMoved(p);
  });
  it("rejects recipient balance overflow before commit", async () => {
    const p = await harness();
    p.transfers.store.setAccountBalance("B", Number.MAX_SAFE_INTEGER);
    const id = await request(p, args({ amount_minor: 1 }));
    const result = await p.transfers.authenticateAndResume(id, "123456", context());
    expect(payload(result).status).not.toBe("SUCCEEDED");
    noMoneyMoved(p);
  });
});

describe("review: trusted identity and authorization", () => {
  it.each(["approve", "reject", "modify"])("foreign tenant cannot %s someone else's request by ID", async operation => {
    const p = await harness();
    const id = await request(p, args({ to_account: "R" }));
    const stranger = context({ userId: "trainer-2", tenantId: "tenant_b", roles: [] });
    if (operation === "approve") await p.transfers.approveTransfer(id, "stranger", stranger);
    if (operation === "reject") await p.transfers.rejectTransfer(id, "stranger", stranger);
    if (operation === "modify") await p.transfers.modifyTransfer(id, { amount_minor: 20000 }, "stranger", stranger);
    expect(p.transfers.store.getRequest(id)!.status).toBe("WAITING_APPROVAL");
    noMoneyMoved(p);
  });
  it.each([
    { roles: ["support"] }, { mode: "plan" }, { agentName: "intern-agent" },
  ])("rechecks the current resume context %j", async overrides => {
    const p = await harness();
    const id = await request(p);
    const result = await p.transfers.authenticateAndResume(id, "123456", context(overrides));
    expect(result.ok).toBe(false);
    noMoneyMoved(p);
  });
  it("rejects authentication records bound to a different identity", async () => {
    const p = await harness();
    const key = randomUUID();
    const first = await submit(p, args(), context(), key);
    const id = payload(first).transfer_request_id!;
    p.transfers.store.insertAuthRecord({ requestId: id, userId: "trainer-2", tenantId: "tenant_b", argsDigest: p.transfers.store.getRequest(id)!.args_digest, ttlSeconds: 300 });
    const result = await submit(p, args(), context(), key);
    expect(payload(result).status).not.toBe("SUCCEEDED");
    noMoneyMoved(p);
  });
  it("rejects approval digest mismatch on resume", async () => {
    const p = await harness();
    const id = await request(p, args({ to_account: "R" }));
    await p.transfers.approveTransfer(id, "teacher", context());
    const approvalId = p.transfers.store.getRequest(id)!.approval_id!;
    // Simulate stale/misbound persisted approval; execution must validate its binding.
    p.store.database.prepare("UPDATE approvals SET args_digest = ? WHERE id = ?").run("wrong-digest", approvalId);
    const result = await p.transfers.authenticateAndResume(id, "123456", context());
    expect(payload(result).status).not.toBe("SUCCEEDED");
    noMoneyMoved(p);
  });
  it("checks auth expiry at execution, after waiting to acquire the transaction", async () => {
    const p = await harness();
    const id = await request(p);
    const original = p.transfers.store.transaction.bind(p.transfers.store);
    const later = new Date(Date.now() + 301000);
    vi.spyOn(p.transfers.store, "transaction").mockImplementation(work => {
      // Deterministic simulation of elapsed lock-wait time; no wall-clock sleep.
      vi.useFakeTimers({ toFake: ["Date"] });
      vi.setSystemTime(later);
      return original(work);
    });
    const result = await p.transfers.authenticateAndResume(id, "123456", context());
    expect(payload(result).status).not.toBe("SUCCEEDED");
    noMoneyMoved(p);
  });
});

describe("review: recovery and failure handling", () => {
  it("coding-agent accepts a second explicit user run but rejects a second call inside one run", async () => {
    const p = await harness();
    vi.spyOn(platformModule, "buildPlatform").mockResolvedValue(p);
    const handlers = new Map<string, (event: unknown, ctx: unknown) => unknown>();
    const registered = new Map<string, { execute: (id: string, args: Record<string, unknown>) => Promise<{ content: Array<{ text: string }> }> }>();
    const flags = new Map<string, unknown>();
    const host = {
      registerProvider() {},
      registerFlag(name: string, definition: { default: unknown }) { flags.set(name, definition.default); },
      getFlag(name: string) { return flags.get(name); },
      on(name: string, handler: (event: unknown, ctx: unknown) => unknown) { handlers.set(name, handler); },
      registerTool(tool: { name: string; execute: (id: string, args: Record<string, unknown>) => Promise<{ content: Array<{ text: string }> }> }) { registered.set(tool.name, tool); },
      async setModel() { return true; },
    };
    managedExtension(host as unknown as ExtensionAPI);
    await handlers.get("session_start")!({}, { ui: { notify() {} } });
    const tool = registered.get("account__transfer")!;
    await handlers.get("agent_start")!({}, {});
    const first = JSON.parse((await tool.execute("user1-call1", args())).content[0].text);
    expect(first.content.status).toBe("WAITING_AUTH");
    const retry = JSON.parse((await tool.execute("user1-call2", args())).content[0].text);
    expect(retry.code).toBe("TRANSFER_ALREADY_REQUESTED");
    await p.transfers.authenticateAndResume(first.content.transfer_request_id, "123456", context());
    await handlers.get("agent_start")!({}, {});
    const second = JSON.parse((await tool.execute("user2-call1", args())).content[0].text);
    expect(second.content.status).toBe("WAITING_AUTH");
    expect(second.content.transfer_request_id).not.toBe(first.content.transfer_request_id);
  });
  it("audit does not retain raw account IDs embedded in caller-supplied idempotency keys", async () => {
    const p = await harness();
    const from = "6222021234567890123";
    const to = "6222029876543210987";
    p.store.database.prepare("UPDATE accounts SET account_id = ? WHERE account_id = 'A'").run(from);
    p.store.database.prepare("UPDATE accounts SET account_id = ? WHERE account_id = 'B'").run(to);
    const key = `demo:${from}:${to}:10000`;
    const result = await submit(p, args({ from_account: from, to_account: to }), context(), key);
    expect(payload(result).status).toBe("WAITING_AUTH");
    p.transfers.statusQuery(key, context());
    const audit = JSON.stringify(p.store.listAudit());
    expect(audit).not.toContain(from);
    expect(audit).not.toContain(to);
  });
  it("a Schema failure cannot be autonomously retried with corrected args in the same run", async () => {
    const p = await harness();
    const ctx = context();
    expect((await submit(p, args({ amount_minor: 0.5 }), ctx)).code).toBe("INVALID_ARGUMENT");
    const retry = await submit(p, args(), ctx);
    expect(retry.code).toBe("TRANSFER_ALREADY_REQUESTED");
    expect(p.transfers.store.listRequests()).toHaveLength(0);
  });
  it("a post-commit timeout returns request ID and a structured unknown outcome", async () => {
    const p = await harness();
    const id = await request(p);
    const saved = process.env.TRANSFER_FAULT;
    try {
      process.env.TRANSFER_FAULT = "post_commit_hang";
      const result = await p.transfers.authenticateAndResume(id, "123456", context());
      expect(p.transfers.store.getRequest(id)!.status).toBe("SUCCEEDED");
      expect(payload(result)).toMatchObject({ transfer_request_id: id, status: "OUTCOME_UNKNOWN", retryable: false });
    } finally {
      if (saved === undefined) delete process.env.TRANSFER_FAULT;
      else process.env.TRANSFER_FAULT = saved;
    }
  });
  it("rolls back a credit failure, including the debit and transaction record", async () => {
    const p = await harness();
    const id = await request(p);
    vi.spyOn(p.transfers.store, "creditAccount").mockImplementation(() => { throw new Error("review: credit unavailable"); });
    const result = await p.transfers.authenticateAndResume(id, "123456", context());
    expect(payload(result).status).toBe("EXECUTION_FAILED");
    noMoneyMoved(p);
  });
  it("failure finalization cannot overwrite a request superseded after rollback", async () => {
    const p = await harness();
    const id = await request(p);
    const transaction = p.transfers.store.transaction.bind(p.transfers.store);
    vi.spyOn(p.transfers.store, "creditAccount").mockImplementation(() => { throw new Error("credit failed"); });
    vi.spyOn(p.transfers.store, "transaction").mockImplementationOnce(work => {
      try { return transaction(work); }
      catch (error) {
        // A different controlled entry wins after ROLLBACK releases the write lock.
        p.transfers.store.supersedeRequest(id, ["WAITING_AUTH"]);
        throw error;
      }
    });
    await p.transfers.authenticateAndResume(id, "123456", context());
    expect(p.transfers.store.getRequest(id)!.status).toBe("SUPERSEDED");
    noMoneyMoved(p);
  });
  it("keeps committed success if audit writing fails", async () => {
    const p = await harness();
    const id = await request(p);
    vi.spyOn(p.store, "addAudit").mockImplementation(() => { throw new Error("review: audit unavailable"); });
    const result = await p.transfers.authenticateAndResume(id, "123456", context());
    expect(payload(result).status).toBe("SUCCEEDED");
    expect(p.transfers.store.countTransactions()).toBe(1);
  });
  it("generic approval entry must lead to a recoverable transfer", async () => {
    const p = await harness();
    const id = await request(p, args({ to_account: "R" }));
    const approvalId = p.transfers.store.getRequest(id)!.approval_id!;
    p.store.decideApproval(approvalId, "approve", "teacher");
    await p.runtime.executeApproval(approvalId);
    expect(p.transfers.store.getRequest(id)!.status).toBe("WAITING_AUTH");
  });
  it("generic operator rejection also terminates the transfer request", async () => {
    const p = await harness();
    const id = await request(p, args({ to_account: "R" }));
    const approvalId = p.transfers.store.getRequest(id)!.approval_id!;
    await p.runtime.decideApproval(approvalId, "reject", "teacher");
    expect(p.transfers.store.getRequest(id)!.status).toBe("REJECTED");
    expect(payload(await p.transfers.authenticateAndResume(id, "123456", context())).status).toBe("REJECTED");
    noMoneyMoved(p);
  });
});

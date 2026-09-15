import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { buildPlatform, executionContext } from "../../src/platform.js";
import { runManagedAgent } from "../../src/pi/agent-runner.js";

// Real Gateway / DeepSeek only. No model mocks or access to the user's demo database.
const observations: unknown[] = [];
afterAll(() => {
  if (process.env.TRANSFER_REVIEW_REPORT) {
    writeFileSync(process.env.TRANSFER_REVIEW_REPORT, JSON.stringify(observations, null, 2));
  }
});
const cases = [
  { name: "normal 100 yuan", to: "B", yuan: "100", minor: 10000, state: "WAITING_AUTH" },
  { name: "minimum 0.01 yuan", to: "B", yuan: "0.01", minor: 1, state: "WAITING_AUTH" },
  { name: "risky recipient", to: "R", yuan: "100", minor: 10000, state: "WAITING_APPROVAL" },
  { name: "missing recipient", to: "X", yuan: "100", minor: 10000, state: "WAITING_MODIFICATION" },
  { name: "insufficient balance", to: "B", yuan: "100", minor: 10000, state: "PRECHECK_FAILED", balance: 5000 },
];
describe("real DeepSeek transfer acceptance", () => {
  it.each(cases)("$name", async scenario => {
    for (const name of ["DEEPSEEK_API_KEY", "GATEWAY_API_KEY"]) {
      expect(process.env[name], `LIVE_TEST_CREDENTIAL_MISSING: ${name}`).toBeTruthy();
    }
    const dir = mkdtempSync(join(tmpdir(), "transfer-review-live-"));
    const p = await buildPlatform({ configPath: resolve("../config/platform.yaml"), connectMcp: false, databaseOverride: join(dir, "test.db") });
    try {
      p.transfers.seedDemoAccounts();
      if (scenario.balance !== undefined) p.transfers.store.setAccountBalance("A", scenario.balance);
      const result = await runManagedAgent(p, {
        prompt: `请实际调用模拟转账工具，从账户 A 向账户 ${scenario.to} 转账 ${scenario.yuan} 元人民币。不要自行更改账户或金额。若需要人工处理，请告诉我请求编号和下一步；若失败，请说明实际错误，不要重试。`,
        userId: "trainer-1", tenantId: "tenant_a", roles: ["supervisor"],
      });
      const requests = p.transfers.store.listRequests();
      const req = requests[0];
      const observation: Record<string, unknown> = {
        scenario: scenario.name, agent: result,
        requests: requests.map(r => ({ id: r.id, status: r.status, amount_minor: r.amount_minor, to: r.to_account, error_code: r.error_code })),
        transactionsBeforeHuman: p.transfers.store.countTransactions(),
        eventTypes: [...new Set(p.store.getTrace(result.trace_id).map(e => e.event_type))],
      };
      observations.push(observation);
      expect(requests).toHaveLength(1);
      expect(req).toMatchObject({ amount_minor: scenario.minor, to_account: scenario.to, status: scenario.state });
      expect(p.transfers.store.countTransactions()).toBe(0);
      // Exercise actual controlled recovery as well as the real model-generated call.
      const ctx = () => executionContext({ traceId: randomUUID(), requestId: randomUUID(), userId: "trainer-1", tenantId: "tenant_a", roles: ["supervisor"] });
      let authId = req.id;
      if (scenario.state === "WAITING_APPROVAL") await p.transfers.approveTransfer(req.id, "reviewer", ctx());
      if (scenario.state === "WAITING_MODIFICATION") {
        const modified: any = await p.transfers.modifyTransfer(req.id, { to_account: "B" }, "reviewer", ctx());
        authId = (modified.payload ?? modified.content).transfer_request_id;
      }
      if (scenario.state.startsWith("WAITING_")) {
        const resumed: any = await p.transfers.authenticateAndResume(authId, "123456", ctx());
        observation.afterHuman = resumed.payload ?? resumed.content;
        expect(observation.afterHuman).toMatchObject({ status: "SUCCEEDED" });
        expect(p.transfers.store.countTransactions()).toBe(1);
      }
      observation.balanceA = p.transfers.store.getAccount("A")!.balance_minor;
      console.log(JSON.stringify(observation));
      expect(result.budget.total_tokens).toBeGreaterThan(0);
      // Waiting output must actually reach the user, not just the internal tool message.
      if (scenario.state.startsWith("WAITING_")) {
        expect(result.final_answer, "user needs the request ID to continue").toContain(req.id);
      } else {
        expect(result.final_answer).toMatch(/余额|不足/);
      }
    } finally {
      await p.close();
      p.store.database.close();
      rmSync(dir, { recursive: true, force: true });
    }
  }, 120000);
});

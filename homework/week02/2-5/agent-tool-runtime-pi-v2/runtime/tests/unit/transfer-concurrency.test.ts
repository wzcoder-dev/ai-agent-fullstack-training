import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { buildPlatform, executionContext, type Platform } from "../../src/platform.js";
import type { InvocationResult } from "../../src/runtime.js";

const CONFIG = resolve(process.cwd(), "../config/platform.yaml");
const WORKER = resolve(process.cwd(), "tests/fixtures/transfer-worker.ts");
const platforms: Platform[] = [];
const databases: string[] = [];

afterAll(async () => {
  for (const platform of platforms) await platform.close();
});

async function seededHarness(): Promise<{ platform: Platform; db: string }> {
  const directory = mkdtempSync(join(tmpdir(), "pi-transfer-concurrency-"));
  const db = join(directory, "platform.db");
  const platform = await buildPlatform({ configPath: CONFIG, connectMcp: false, databaseOverride: db });
  platform.transfers.seedDemoAccounts(true);
  platforms.push(platform);
  databases.push(db);
  return { platform, db };
}

interface WorkerOutcome {
  code: string;
  ok: boolean;
  content: Record<string, unknown>;
}

/** 真正的独立进程 + 独立 SQLite 连接：验证数据库层面的竞争控制。 */
function runWorker(db: string, options: Record<string, string>): Promise<WorkerOutcome> {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(process.execPath, ["--import", "tsx", WORKER], {
      cwd: process.cwd(),
      env: { ...process.env, TRANSFER_DB: db, ...options },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.on("close", (exitCode) => {
      const line = stdout.split("\n").find((item) => item.startsWith("RESULT "));
      if (!line) {
        reject(new Error(`worker exited ${exitCode} without RESULT: ${stderr.slice(-400)}`));
        return;
      }
      const result = JSON.parse(line.slice("RESULT ".length)) as InvocationResult & { payload?: Record<string, unknown> };
      // 认证入口可能返回 TransferResult（payload）或 InvocationResult（content），统一取业务负载。
      resolvePromise({
        code: result.code,
        ok: result.ok,
        content: (result.content ?? result.payload ?? {}) as Record<string, unknown>,
      });
    });
  });
}

describe("模拟转账：跨进程并发（独立执行入口 + 独立数据库连接）", () => {
  it(
    "同 key 并发提交只产生一笔交易，两个入口得到同一 transaction_id",
    async () => {
      const { platform, db } = await seededHarness();
      const shared = {
        TRANSFER_FROM: "A",
        TRANSFER_TO: "B",
        TRANSFER_AMOUNT_MINOR: "30000",
        TRANSFER_KEY: "concurrent-same-key",
        TRANSFER_AUTH: "1",
        TRANSFER_TX_DELAY_MS: "200", // 持有写锁 200ms，放大竞争窗口
      };
      const [first, second] = await Promise.all([runWorker(db, shared), runWorker(db, shared)]);

      // 至少一个入口拿到已提交交易；另一入口允许停留在合法的瞬时/等待视图
      // （spec：处理中重复请求返回当前状态，不再次进入资金执行）。
      const outcomes = [first, second].map((outcome) => String(outcome.content.status));
      const executingViews = ["SUCCEEDED"];
      const transientViews = [
        "EXECUTING",
        "DUPLICATE_IN_PROGRESS",
        "WAITING_AUTH",
        "CREATED",
        "VALIDATING",
        "POLICY_CHECKING",
        "PRECHECKING",
        "RISK_CHECKING",
      ];
      expect(outcomes.filter((status) => executingViews.includes(status)).length).toBeGreaterThanOrEqual(1);
      for (const status of outcomes) {
        expect([...executingViews, ...transientViews]).toContain(status);
      }

      expect(platform.transfers.store.countTransactions()).toBe(1);
      const accountA = platform.transfers.store.getAccount("A")!;
      const accountB = platform.transfers.store.getAccount("B")!;
      expect(accountA.balance_minor).toBe(1_000_000 - 30_000);
      expect(accountB.balance_minor).toBe(30_000);

      // 至少一个入口拿到已提交交易；两个入口拿到的 transaction_id 一致（若都有）。
      const transactionIds = [first, second]
        .map((outcome) => outcome.content.transaction_id)
        .filter(Boolean);
      expect(new Set(transactionIds).size).toBe(1);
    },
    60_000,
  );

  it(
    "不同 key 并发转账不会透支，也不会突破每日额度",
    async () => {
      const { platform, db } = await seededHarness();
      platform.transfers.store.setAccountBalance("A", 1_500_000);
      // 每日上限 1000000 分：三笔 400000 并发，事务内串行复检后恰好只能成功两笔。
      const options = {
        TRANSFER_FROM: "A",
        TRANSFER_TO: "B",
        TRANSFER_AMOUNT_MINOR: "400000",
        TRANSFER_AUTH: "1",
        TRANSFER_TX_DELAY_MS: "150",
      };
      const [first, second, third] = await Promise.all([
        runWorker(db, { ...options, TRANSFER_KEY: "concurrent-distinct-1" }),
        runWorker(db, { ...options, TRANSFER_KEY: "concurrent-distinct-2" }),
        runWorker(db, { ...options, TRANSFER_KEY: "concurrent-distinct-3" }),
      ]);

      const succeeded = [first, second, third].filter((outcome) => outcome.content.status === "SUCCEEDED");
      const blocked = [first, second, third].filter((outcome) => outcome.content.status !== "SUCCEEDED");
      expect(succeeded.length).toBe(2); // 400000 × 2 = 800000 ≤ 每日 1000000
      expect(blocked.length).toBe(1);
      expect(blocked[0].code).toBe("DAILY_LIMIT_EXCEEDED");

      const accountA = platform.transfers.store.getAccount("A")!;
      expect(accountA.balance_minor).toBe(1_500_000 - 800_000); // 不透支、不超每日额度
      expect(platform.transfers.store.countTransactions()).toBe(2);

      // 每日已转出总额 = 已提交流水之和。
      const daily = platform.transfers.store.sumSuccessfulOutgoing("A", new Date().toISOString().slice(0, 10));
      expect(daily).toBe(800_000);
    },
    60_000,
  );
});

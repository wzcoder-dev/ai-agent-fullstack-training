/**
 * 并发测试 worker：独立进程、独立 SQLite 连接，通过受治理的 Runtime 入口执行一次转账。
 * 环境变量：
 *   TRANSFER_DB          数据库文件路径（父进程已初始化账户）
 *   TRANSFER_FROM / TRANSFER_TO / TRANSFER_AMOUNT_MINOR / TRANSFER_KEY
 *   TRANSFER_AUTH=1      提交后立即认证并恢复执行
 *   TRANSFER_TX_DELAY_MS 事务内同步等待（放大写锁竞争窗口）
 * 输出单行 "RESULT {json}" 供父进程解析。
 */
import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import { buildPlatform, executionContext } from "../../src/platform.js";

const configPath = resolve(process.cwd(), "../config/platform.yaml");
const platform = await buildPlatform({
  configPath,
  connectMcp: false,
  databaseOverride: process.env.TRANSFER_DB!,
});
try {
  const context = executionContext({
    traceId: randomUUID(),
    requestId: randomUUID(),
    userId: process.env.TRANSFER_USER ?? "trainer-1",
    tenantId: process.env.TRANSFER_TENANT ?? "tenant_a",
    roles: ["supervisor"],
  });
  const argsValue = {
    from_account: process.env.TRANSFER_FROM!,
    to_account: process.env.TRANSFER_TO!,
    amount_minor: Number(process.env.TRANSFER_AMOUNT_MINOR!),
    currency: "CNY",
  };
  const submitted = await platform.runtime.invoke({
    context,
    toolCallId: randomUUID(),
    modelName: "account__transfer",
    args: argsValue,
    idempotencyKey: process.env.TRANSFER_KEY!,
  });
  let final = submitted;
  const requestId = (submitted.content as { transfer_request_id?: string })?.transfer_request_id;
  if (process.env.TRANSFER_AUTH === "1" && requestId) {
    const authResult = await platform.transfers.authenticateAndResume(
      requestId,
      process.env.TRANSFER_PASSWORD ?? "123456",
      executionContext({
        traceId: randomUUID(),
        requestId: randomUUID(),
        userId: context.userId,
        tenantId: context.tenantId,
        roles: ["supervisor"],
      }),
    );
    final = authResult as typeof submitted;

    // 并发竞争下可能读到瞬态（CREATED/PRECHECKING/EXECUTING/WAITING_AUTH 等）。
    // 真实调用方应对 in-flight 重复做有界轮询直到稳定终态，这里同样处理。
    const businessOf = (value: unknown) =>
      ((value as { payload?: unknown }).payload ?? (value as { content?: unknown }).content ?? {}) as {
        status?: string;
      };
    let business = businessOf(final);
    const stable = new Set([
      "SUCCEEDED",
      "REJECTED",
      "AUTH_FAILED",
      "EXECUTION_FAILED",
      "POLICY_DENIED",
      "PRECHECK_FAILED",
      "VALIDATION_FAILED",
      "SUPERSEDED",
    ]);
    for (let attempt = 0; attempt < 30 && !stable.has(String(business.status)); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      const queried = platform.transfers.statusQuery(requestId, context);
      business = queried.payload as { status?: string };
      final = { ...final, ok: queried.ok, code: queried.code, content: queried.payload } as typeof final;
    }
  }
  process.stdout.write(`RESULT ${JSON.stringify(final)}\n`);
} finally {
  await platform.close();
}

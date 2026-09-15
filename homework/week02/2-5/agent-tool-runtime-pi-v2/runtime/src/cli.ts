#!/usr/bin/env node
import { createHash, randomUUID } from "node:crypto";
import { Command } from "commander";
import { buildPlatform, executionContext } from "./platform.js";
import { runManagedAgent } from "./pi/agent-runner.js";

const program = new Command();
program.name("agent-runtime").description("pi 0.83.0 Agent Tool Runtime CLI").version("2.0.0");
program.option("--config <path>", "平台配置文件", process.env.PLATFORM_CONFIG ?? "../config/platform.yaml");

program
  .command("tools")
  .description("发现并列出模型当前可见的工具")
  .option("--without-mcp", "不连接正式 MCP 服务器")
  .action(async (options) => {
    await withPlatform(!options.withoutMcp, async (platform) => {
      const context = defaultContext();
      print(platform.registry.snapshot(context, platform.policy).map((tool) => ({
        internal_name: tool.metadata.internalName,
        model_name: tool.modelName,
        risk: tool.metadata.risk,
        source: tool.metadata.source,
        requires_confirmation: tool.metadata.requiresConfirmation,
      })));
    });
  });

program
  .command("agent")
  .description("使用真实 DeepSeek 运行受治理的 pi Agent Loop")
  .argument("<prompt>", "用户任务")
  .option("--user-id <id>", "用户 ID", "trainer-1")
  .option("--tenant-id <id>", "租户 ID", "tenant_a")
  .option("--roles <roles>", "逗号分隔角色", "supervisor")
  .option("--mode <mode>", "plan 或 execute", "execute")
  .option("--without-mcp", "不连接正式 MCP 服务器")
  .action(async (prompt, options) => {
    await withPlatform(!options.withoutMcp, async (platform) => {
      const result = await runManagedAgent(platform, {
        prompt,
        userId: options.userId,
        tenantId: options.tenantId,
        roles: splitList(options.roles),
        mode: options.mode,
        onText: (delta) => process.stdout.write(delta),
      });
      process.stdout.write("\n");
      print(result);
    });
  });

const approvals = program.command("approvals").description("查看与处理持久化审批");
approvals
  .command("list")
  .option("--status <status>", "按状态过滤", "pending")
  .action(async (options) => {
    await withPlatform(false, async (platform) => print(platform.store.listApprovals(options.status)));
  });
approvals
  .command("decide")
  .argument("<approval-id>")
  .requiredOption("--decision <decision>", "approve 或 reject")
  .option("--actor <actor>", "审批人", process.env.USER ?? "operator")
  .action(async (id, options) => {
    if (options.decision !== "approve" && options.decision !== "reject") {
      throw new Error("--decision 只能是 approve 或 reject");
    }
    await withPlatform(false, async (platform) => {
      print(await platform.runtime.decideApproval(id, options.decision, options.actor));
    });
  });
approvals
  .command("execute")
  .argument("<approval-id>")
  .option("--without-mcp", "审批目标不是 MCP 工具时可跳过远端连接")
  .action(async (id, options) => {
    await withPlatform(!options.withoutMcp, async (platform) => print(await platform.runtime.executeApproval(id)));
  });

program
  .command("audit")
  .description("查看审计记录")
  .option("--trace-id <id>")
  .action(async (options) => {
    await withPlatform(false, async (platform) => print(platform.store.listAudit(options.traceId)));
  });

program
  .command("trace")
  .description("查看一次 Agent Run 的 Trace")
  .argument("<trace-id>")
  .action(async (traceId) => {
    await withPlatform(false, async (platform) => print(platform.store.getTrace(traceId)));
  });

program
  .command("invoke")
  .description("绕过模型但不绕过 Runtime，便于确定性验收")
  .argument("<model-tool-name>")
  .argument("<json-args>")
  .option("--with-mcp", "连接正式 MCP 服务器")
  .option("--user-id <id>", "用户 ID", "trainer-1")
  .option("--tenant-id <id>", "租户 ID", "tenant_a")
  .option("--roles <roles>", "逗号分隔角色", "supervisor")
  .option("--agent-name <name>", "Agent 名称", "support-agent")
  .option("--mode <mode>", "plan 或 execute", "execute")
  .option("--idempotency-key <key>", "业务幂等键（可信入口提供，模型不可见）")
  .action(async (modelName, rawArgs, options) => {
    await withPlatform(Boolean(options.withMcp), async (platform) => {
      const result = await platform.runtime.invoke({
        context: executionContext({
          traceId: randomUUID(),
          requestId: randomUUID(),
          userId: options.userId,
          tenantId: options.tenantId,
          roles: splitList(options.roles),
          agentName: options.agentName,
          mode: options.mode === "plan" ? "plan" : "execute",
        }),
        toolCallId: randomUUID(),
        modelName,
        args: parseObject(rawArgs),
        idempotencyKey: options.idempotencyKey,
      });
      print(result);
    });
  });

// ---------------- 模拟账户与转账 ----------------

const accounts = program.command("accounts").description("模拟账户演示数据");
accounts
  .command("init")
  .description("初始化演示账户（A/B/R/FOREIGN/CBANK）。--force 显式重置全部转账数据")
  .option("--force", "清空账户、请求、流水与认证记录后重新写入")
  .action(async (options) => {
    await withPlatform(false, async (platform) => {
      print(platform.transfers.seedDemoAccounts(Boolean(options.force)));
    });
  });
accounts
  .command("list")
  .description("查看模拟账户与余额（整数分）")
  .action(async () => {
    await withPlatform(false, async (platform) => print(platform.transfers.store.listAccounts()));
  });

function identityOptions(command: Command, defaults = { roles: "supervisor" }): Command {
  return command
    .option("--user-id <id>", "用户 ID", "trainer-1")
    .option("--tenant-id <id>", "租户 ID", "tenant_a")
    .option("--roles <roles>", "逗号分隔角色", defaults.roles)
    .option("--agent-name <name>", "Agent 名称", "support-agent")
    .option("--mode <mode>", "plan 或 execute", "execute");
}

const transfer = program.command("transfer").description("模拟转账：受治理的确定性入口");
identityOptions(
  transfer
    .command("submit")
    .description("发起转账请求（金额单位为元，最多两位小数）")
    .argument("<from-account>")
    .argument("<to-account>")
    .argument("<amount-yuan>")
    .option("--key <key>", "业务幂等键；缺省按“用户+租户+账户+金额”确定生成"),
).action(async (from, to, amountYuan, options) => {
  await withPlatform(false, async (platform) => {
    const context = cliContext(options);
    const amountMinor = parseYuanToMinor(amountYuan);
    const key = options.key ?? `demo:${createHash("sha256").update(JSON.stringify([options.tenantId, options.userId, from, to, amountMinor])).digest("hex")}`;
    const result = await platform.runtime.invoke({
      context,
      toolCallId: randomUUID(),
      modelName: "account__transfer",
      args: { from_account: from, to_account: to, amount_minor: amountMinor, currency: "CNY" },
      idempotencyKey: key,
    });
    print(result);
  });
});

transfer
  .command("list")
  .description("列出转账请求（默认等待中）")
  .option("--status <status>", "按状态过滤", "WAITING_APPROVAL")
  .action(async (options) => {
    await withPlatform(false, async (platform) => print(platform.transfers.store.listRequests(options.status)));
  });

identityOptions(
  transfer
    .command("approve")
    .description("批准风险账户转账（批准后仍需支付认证）")
    .argument("<request-or-approval-id>")
    .option("--actor <actor>", "审批人", process.env.USER ?? "operator"),
).action(async (identifier, options) => {
  await withPlatform(false, async (platform) => {
    print(await platform.transfers.approveTransfer(identifier, options.actor, cliContext(options)));
  });
});

identityOptions(
  transfer
    .command("reject")
    .description("拒绝转账请求（无资金副作用）")
    .argument("<request-or-approval-id>")
    .option("--actor <actor>", "审批人", process.env.USER ?? "operator"),
).action(async (identifier, options) => {
  await withPlatform(false, async (platform) => {
    print(await platform.transfers.rejectTransfer(identifier, options.actor, cliContext(options)));
  });
});

identityOptions(
  transfer
    .command("modify")
    .description("修改等待中的请求：生成新请求并取代旧请求")
    .argument("<request-id>")
    .option("--to <account>", "新的收款账户")
    .option("--amount-yuan <amount>", "新的金额（元）")
    .option("--actor <actor>", "操作人", process.env.USER ?? "operator"),
).action(async (requestId, options) => {
  await withPlatform(false, async (platform) => {
    const changes: { to_account?: string; amount_minor?: number } = {};
    if (options.to) changes.to_account = options.to;
    if (options.amountYuan) changes.amount_minor = parseYuanToMinor(options.amountYuan);
    print(await platform.transfers.modifyTransfer(requestId, changes, options.actor, cliContext(options)));
  });
});

transfer
  .command("auth")
  .description("隐藏输入模拟支付密码（演示密码见 config/platform.yaml，仅模拟）并恢复执行")
  .argument("<request-id>")
  .option("--user-id <id>", "用户 ID", "trainer-1")
  .option("--tenant-id <id>", "租户 ID", "tenant_a")
  .action(async (requestId, options) => {
    await withPlatform(false, async (platform) => {
      const password = await readPassword("支付密码（模拟，输入不回显）: ");
      const result = await platform.transfers.authenticateAndResume(
        requestId,
        password,
        executionContext({
          traceId: randomUUID(),
          requestId: randomUUID(),
          userId: options.userId,
          tenantId: options.tenantId,
          roles: ["supervisor"],
        }),
      );
      print(result);
    });
  });

transfer
  .command("status")
  .description("按请求 ID 或幂等键查询结果（只读，不触发转账）")
  .argument("<request-or-key>")
  .option("--user-id <id>", "用户 ID", "trainer-1")
  .option("--tenant-id <id>", "租户 ID", "tenant_a")
  .action(async (identifier, options) => {
    await withPlatform(false, async (platform) => {
      print(
        platform.transfers.statusQuery(
          identifier,
          executionContext({
            traceId: randomUUID(),
            requestId: randomUUID(),
            userId: options.userId,
            tenantId: options.tenantId,
            roles: ["supervisor"],
          }),
        ),
      );
    });
  });

await program.parseAsync();

function defaultContext() {
  return executionContext({
    traceId: randomUUID(),
    requestId: randomUUID(),
    userId: "trainer-1",
    tenantId: "tenant_a",
    roles: ["supervisor"],
  });
}

async function withPlatform(connectMcp: boolean, work: (platform: Awaited<ReturnType<typeof buildPlatform>>) => Promise<void>) {
  const platform = await buildPlatform({ configPath: program.opts().config, connectMcp });
  try {
    await work(platform);
  } finally {
    await platform.close();
  }
}

function splitList(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function cliContext(options: {
  userId?: string;
  tenantId?: string;
  roles?: string;
  agentName?: string;
  mode?: string;
}) {
  return executionContext({
    traceId: randomUUID(),
    requestId: randomUUID(),
    userId: options.userId ?? "trainer-1",
    tenantId: options.tenantId ?? "tenant_a",
    roles: splitList(options.roles ?? "supervisor"),
    agentName: options.agentName,
    mode: options.mode === "plan" ? "plan" : "execute",
  });
}

/** 元转分：只接受最多两位小数的十进制字符串，避免浮点误差进入资金计算。 */
function parseYuanToMinor(value: string): number {
  if (!/^\d+(\.\d{1,2})?$/.test(value)) {
    throw new Error(`金额格式错误：${value}（应为元，最多两位小数）`);
  }
  const [whole, fraction = ""] = value.split(".");
  return Number(whole) * 100 + Number(fraction.padEnd(2, "0") || "0");
}

/** 隐藏输入读取密码：不经命令行参数（不进 shell history），不回显到终端。 */
async function readPassword(prompt: string): Promise<string> {
  const input = process.stdin;
  if (!input.isTTY) throw new Error("支付认证必须在交互式终端输入");
  const wasRaw = input.isRaw;
  let cleanup = () => {};
  try {
    input.setRawMode(true);
    input.resume();
    return await new Promise<string>((resolve, reject) => {
      let password = "";
      const cancel = () => reject(new Error("支付认证已取消"));
      const onData = (chunk: Buffer) => {
        for (const char of chunk.toString("utf8")) {
          if (char === "\r" || char === "\n") { resolve(password); return; }
          if (char === "\u0003" || char === "\u0004") { cancel(); return; }
          if (char === "\u007f" || char === "\b") password = [...password].slice(0, -1).join("");
          else if (char >= " ") password += char;
        }
      };
      input.on("data", onData);
      input.once("end", cancel);
      process.once("SIGTERM", cancel);
      cleanup = () => {
        input.off("data", onData);
        input.off("end", cancel);
        process.off("SIGTERM", cancel);
        password = "";
      };
      process.stderr.write(prompt);
    });
  } finally {
    cleanup();
    input.setRawMode(wasRaw);
    input.pause();
    process.stderr.write("\n");
  }
}

function parseObject(value: string): Record<string, unknown> {
  const parsed = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("参数必须是 JSON 对象");
  return parsed;
}

function print(value: unknown): void {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

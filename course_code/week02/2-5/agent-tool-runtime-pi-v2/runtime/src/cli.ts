#!/usr/bin/env node
import { randomUUID } from "node:crypto";
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
      print(platform.store.decideApproval(id, options.decision, options.actor));
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
  .action(async (modelName, rawArgs, options) => {
    await withPlatform(Boolean(options.withMcp), async (platform) => {
      const result = await platform.runtime.invoke({
        context: defaultContext(),
        toolCallId: randomUUID(),
        modelName,
        args: parseObject(rawArgs),
      });
      print(result);
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

function parseObject(value: string): Record<string, unknown> {
  const parsed = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("参数必须是 JSON 对象");
  return parsed;
}

function print(value: unknown): void {
  process.stdout.write(`${JSON.stringify(value, null, 2)}\n`);
}

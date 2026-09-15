import type { ExecutionContext } from "./domain.js";
import { loadConfig, type PlatformConfig } from "./config.js";
import { McpAdapterPlugin } from "./mcp/adapter.js";
import { PolicyEngine } from "./policy.js";
import { ToolRegistry } from "./registry.js";
import { ToolRuntime } from "./runtime.js";
import { PlatformStore } from "./store.js";
import { createOrderTool } from "./tools/order.js";
import { createTicketTool } from "./tools/ticket.js";
import { TransferPolicy } from "./transfer/policy.js";
import { TransferService } from "./transfer/service.js";
import { TransferStore } from "./transfer/store.js";
import { createTransferTool } from "./transfer/tool.js";

export interface Platform {
  config: PlatformConfig;
  store: PlatformStore;
  registry: ToolRegistry;
  policy: PolicyEngine;
  runtime: ToolRuntime;
  transfers: TransferService;
  mcp?: McpAdapterPlugin;
  close(): Promise<void>;
}

export async function buildPlatform(
  options: { configPath?: string; connectMcp?: boolean; databaseOverride?: string } = {},
): Promise<Platform> {
  const config = loadConfig(options.configPath);
  if (options.databaseOverride) config.runtime.database = options.databaseOverride;
  const store = new PlatformStore(config.runtime.database);
  const policy = new PolicyEngine(config.policy);
  const registry = new ToolRegistry();
  registry.register(createOrderTool(config));
  registry.register(createTicketTool(store));
  const transferStore = new TransferStore(store.database);
  const transferPolicy = new TransferPolicy({
    currency: config.transfer.currency,
    singleLimitMinor: config.transfer.singleLimitMinor,
    dailyLimitMinor: config.transfer.dailyLimitMinor,
  });
  const transfers = new TransferService(store, transferStore, transferPolicy, config.transfer);
  registry.register(createTransferTool(transfers));
  let mcp: McpAdapterPlugin | undefined;
  if (options.connectMcp ?? true) {
    mcp = new McpAdapterPlugin(config.mcpServers);
    await mcp.attach(registry);
  }
  const runtime = new ToolRuntime(registry, policy, store, transfers);
  transfers.bindRuntime(runtime);
  return {
    config,
    store,
    registry,
    policy,
    runtime,
    transfers,
    mcp,
    async close() {
      await mcp?.close();
    },
  };
}

export function executionContext(input: {
  userId: string;
  tenantId: string;
  roles: string[];
  agentName?: string;
  mode?: "plan" | "execute";
  traceId: string;
  requestId: string;
}): ExecutionContext {
  return {
    traceId: input.traceId,
    requestId: input.requestId,
    userId: input.userId,
    tenantId: input.tenantId,
    roles: input.roles,
    agentName: input.agentName ?? "support-agent",
    mode: input.mode ?? "execute",
  };
}

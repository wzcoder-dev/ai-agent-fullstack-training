import type { ExecutionContext } from "./domain.js";
import { loadConfig, type PlatformConfig } from "./config.js";
import { McpAdapterPlugin } from "./mcp/adapter.js";
import { PolicyEngine } from "./policy.js";
import { ToolRegistry } from "./registry.js";
import { ToolRuntime } from "./runtime.js";
import { PlatformStore } from "./store.js";
import { createOrderTool } from "./tools/order.js";
import { createTicketTool } from "./tools/ticket.js";

export interface Platform {
  config: PlatformConfig;
  store: PlatformStore;
  registry: ToolRegistry;
  policy: PolicyEngine;
  runtime: ToolRuntime;
  mcp?: McpAdapterPlugin;
  close(): Promise<void>;
}

export async function buildPlatform(options: { configPath?: string; connectMcp?: boolean } = {}): Promise<Platform> {
  const config = loadConfig(options.configPath);
  const store = new PlatformStore(config.runtime.database);
  const policy = new PolicyEngine(config.policy);
  const registry = new ToolRegistry();
  registry.register(createOrderTool(config));
  registry.register(createTicketTool(store));
  let mcp: McpAdapterPlugin | undefined;
  if (options.connectMcp ?? true) {
    mcp = new McpAdapterPlugin(config.mcpServers);
    await mcp.attach(registry);
  }
  const runtime = new ToolRuntime(registry, policy, store);
  return {
    config,
    store,
    registry,
    policy,
    runtime,
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

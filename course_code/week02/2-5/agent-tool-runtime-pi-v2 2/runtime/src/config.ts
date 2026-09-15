import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { parse } from "yaml";
import type { LoopBudgetConfig } from "./loop-guard.js";
import type { McpServerConfig } from "./mcp/adapter.js";
import type { PolicyConfig } from "./domain.js";

export interface PlatformConfig {
  gateway: {
    baseUrl: string;
    apiKeyEnv: string;
    promptName: string;
    promptVersion: string;
  };
  model: {
    providerId: string;
    id: string;
    name: string;
    contextWindow: number;
    maxTokens: number;
    inputPerMillion: number;
    outputPerMillion: number;
    cacheReadPerMillion: number;
  };
  runtime: LoopBudgetConfig & { database: string };
  approvalApi: { host: string; port: number; apiKeyEnv: string };
  policy: PolicyConfig;
  tools: { orderServiceUrl: string; orderSchema: string };
  mcpServers: McpServerConfig[];
}

export function loadConfig(path = process.env.PLATFORM_CONFIG ?? "../config/platform.yaml"): PlatformConfig {
  const absolutePath = resolve(path);
  const directory = dirname(absolutePath);
  const raw = parse(readFileSync(absolutePath, "utf8")) as any;
  return {
    gateway: {
      baseUrl: process.env.GATEWAY_BASE_URL ?? raw.gateway.base_url,
      apiKeyEnv: raw.gateway.api_key_env,
      promptName: raw.gateway.prompt_name,
      promptVersion: raw.gateway.prompt_version,
    },
    model: {
      providerId: raw.model.provider_id,
      id: raw.model.id,
      name: raw.model.name,
      contextWindow: raw.model.context_window,
      maxTokens: raw.model.max_tokens,
      inputPerMillion: raw.model.input_per_million,
      outputPerMillion: raw.model.output_per_million,
      cacheReadPerMillion: raw.model.cache_read_per_million,
    },
    runtime: {
      database: resolve(directory, raw.runtime.database),
      maxTurns: raw.runtime.max_turns,
      maxToolCalls: raw.runtime.max_tool_calls,
      maxRepeatedCall: raw.runtime.max_repeated_call,
      maxTotalTokens: raw.runtime.max_total_tokens,
      maxCostUsd: raw.runtime.max_cost_usd,
      maxDurationMs: raw.runtime.max_duration_ms,
    },
    approvalApi: {
      host: raw.approval_api.host,
      port: raw.approval_api.port,
      apiKeyEnv: raw.approval_api.api_key_env,
    },
    policy: {
      rolePermissions: raw.policy.role_permissions,
      agentToolAllowlist: raw.policy.agent_tool_allowlist,
      resourceTenantPrefixes: raw.policy.resource_tenant_prefixes,
    },
    tools: {
      orderServiceUrl: raw.tools.order_service_url,
      orderSchema: resolve(directory, raw.tools.order_schema),
    },
    mcpServers: (raw.mcp_servers ?? []).map((server: any): McpServerConfig => {
      const common = {
        id: server.id,
        trust: server.trust,
        defaultRisk: server.default_risk,
        permissions: server.permissions ?? [],
      } as const;
      if (server.type === "streamablehttp") {
        return {
          ...common,
          type: "streamablehttp",
          url: server.url,
          headers: server.headers ?? {},
        };
      }
      return {
        ...common,
        type: "stdio",
        command: server.command,
        args: server.args ?? [],
        cwd: server.cwd ? resolve(directory, server.cwd) : undefined,
      };
    }),
  };
}

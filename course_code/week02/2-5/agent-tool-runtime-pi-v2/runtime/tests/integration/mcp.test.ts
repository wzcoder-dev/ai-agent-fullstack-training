import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import type { ExecutionContext } from "../../src/domain.js";
import { McpAdapterPlugin } from "../../src/mcp/adapter.js";
import { PolicyEngine } from "../../src/policy.js";
import { ToolRegistry } from "../../src/registry.js";
import { ToolRuntime } from "../../src/runtime.js";
import { PlatformStore } from "../../src/store.js";

const adapter = new McpAdapterPlugin([
  {
    id: "demo",
    type: "stdio",
    command: process.execPath,
    args: ["--import", "tsx", "src/mcp/demo-server.ts"],
    trust: "trusted",
    defaultRisk: "read",
    permissions: ["knowledge:read"],
  },
]);

afterAll(async () => {
  await adapter.close();
});

describe("MCP adapter public seam", () => {
  it("discovers a remote tool and executes it through ToolRuntime", async () => {
    const registry = new ToolRegistry();
    await adapter.attach(registry);
    const policy = new PolicyEngine({
      rolePermissions: { support: ["knowledge:read"] },
      agentToolAllowlist: { "support-agent": ["mcp.demo.knowledge_search"] },
      resourceTenantPrefixes: { tenant_a: ["ORD-A-"] },
    });
    const directory = mkdtempSync(join(tmpdir(), "pi-mcp-test-"));
    const store = new PlatformStore(join(directory, "platform.db"));
    const runtime = new ToolRuntime(registry, policy, store);
    const context: ExecutionContext = {
      traceId: "trace-mcp",
      requestId: "request-mcp",
      userId: "user-1",
      tenantId: "tenant_a",
      roles: ["support"],
      agentName: "support-agent",
      mode: "execute",
    };

    const result = await runtime.invoke({
      context,
      toolCallId: "call-mcp",
      modelName: "mcp__demo__knowledge_search",
      args: { query: "退款处理" },
    });

    expect(result.ok).toBe(true);
    expect(JSON.stringify(result.content)).toContain("人工确认");
    expect(store.listAudit("trace-mcp")).toHaveLength(1);
  });
});

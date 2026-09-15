import { describe, expect, it } from "vitest";
import { Type } from "typebox";
import { PolicyEngine } from "../../src/policy.js";
import { ToolRegistry } from "../../src/registry.js";
import type { ExecutionContext, ManagedTool } from "../../src/domain.js";

const context: ExecutionContext = {
  traceId: "trace-registry",
  requestId: "request-registry",
  userId: "user-1",
  tenantId: "tenant_a",
  roles: ["support"],
  agentName: "support-agent",
  mode: "execute",
};

function tool(internalName: string): ManagedTool {
  return {
    metadata: {
      internalName,
      description: `Tool ${internalName}`,
      version: "1.0.0",
      parameters: Type.Object({ value: Type.String() }, { additionalProperties: false }),
      risk: "read",
      permissions: ["order:read"],
      requiresConfirmation: false,
      timeoutMs: 1_000,
      maxRetries: 0,
      idempotent: true,
      source: "local",
    },
    async handler() {
      return { ok: true };
    },
  };
}

describe("ToolRegistry public snapshot", () => {
  it("exposes only allowlisted tools and maps dotted names for DeepSeek", () => {
    const registry = new ToolRegistry();
    registry.register(tool("order.get_status"));
    registry.register(tool("ticket.create"));
    const policy = new PolicyEngine({
      rolePermissions: { support: ["order:read"] },
      agentToolAllowlist: { "support-agent": ["order.get_status"] },
      resourceTenantPrefixes: { tenant_a: ["ORD-A-"] },
    });

    const snapshot = registry.snapshot(context, policy);

    expect(snapshot.map((entry) => entry.modelName)).toEqual(["order__get_status"]);
    expect(registry.getByModelName("order__get_status").metadata.internalName).toBe("order.get_status");
  });

  it("rejects model-name collisions during registration", () => {
    const registry = new ToolRegistry();
    registry.register(tool("order.get_status"));

    expect(() => registry.register(tool("order__get_status"))).toThrow(/collision/i);
  });

  it("allows only the configured my-coffee namespace wildcard", () => {
    const registry = new ToolRegistry();
    registry.register(tool("mcp.my-coffee.order_create"));
    registry.register(tool("mcp.other.order_create"));
    const policy = new PolicyEngine({
      rolePermissions: { support: ["order:read"] },
      agentToolAllowlist: { "support-agent": ["mcp.my-coffee.*"] },
      resourceTenantPrefixes: { tenant_a: ["ORD-A-"] },
    });

    expect(registry.snapshot(context, policy).map((entry) => entry.metadata.internalName)).toEqual([
      "mcp.my-coffee.order_create",
    ]);
  });
});

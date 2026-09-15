import { readFileSync } from "node:fs";
import type { ManagedTool } from "../domain.js";
import { ToolRuntimeError } from "../domain.js";
import type { PlatformConfig } from "../config.js";

export function createOrderTool(config: PlatformConfig): ManagedTool {
  const schema = JSON.parse(readFileSync(config.tools.orderSchema, "utf8"));
  return {
    metadata: {
      internalName: "order.get_status",
      description: "查询当前租户中的订单状态。订单事实必须使用此工具获取。",
      version: "2.0.0",
      parameters: schema,
      risk: "read",
      permissions: ["order:read"],
      requiresConfirmation: false,
      timeoutMs: 3_000,
      maxRetries: 2,
      idempotent: true,
      source: "python",
      sensitiveFields: ["customer_email", "access_token"],
    },
    async handler({ args, context, signal }) {
      const apiKey = process.env[config.gateway.apiKeyEnv];
      if (!apiKey) throw new ToolRuntimeError("GATEWAY_KEY_MISSING", `缺少 ${config.gateway.apiKeyEnv}`, false);
      const response = await fetch(config.tools.orderServiceUrl, {
        method: "POST",
        signal,
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${apiKey}`,
          "x-tenant-id": context.tenantId,
          "x-user-id": context.userId,
          "x-trace-id": context.traceId,
        },
        body: JSON.stringify(args),
      });
      if (response.status >= 500 || response.status === 429) {
        throw new ToolRuntimeError("ORDER_SERVICE_UNAVAILABLE", `订单服务返回 ${response.status}`, true);
      }
      if (!response.ok) {
        throw new ToolRuntimeError("ORDER_LOOKUP_FAILED", await response.text(), false);
      }
      return response.json();
    },
    async fallbackHandler({ args }) {
      return {
        order_id: args.order_id,
        status: "unknown",
        source: "fallback-cache",
        warning: "实时订单服务不可用，请人工核实",
      };
    },
  };
}

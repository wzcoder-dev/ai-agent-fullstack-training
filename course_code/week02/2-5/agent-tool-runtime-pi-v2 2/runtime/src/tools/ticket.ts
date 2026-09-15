import { Type } from "typebox";
import type { ManagedTool } from "../domain.js";
import type { PlatformStore } from "../store.js";

export function createTicketTool(store: PlatformStore): ManagedTool {
  return {
    metadata: {
      internalName: "ticket.create",
      description: "为当前租户创建客服工单。此工具产生写入副作用，必须人工审批。",
      version: "2.0.0",
      parameters: Type.Object(
        {
          order_id: Type.String({ pattern: "^ORD-[A-Z]-[0-9]{4,12}$" }),
          title: Type.String({ minLength: 3, maxLength: 120 }),
        },
        { additionalProperties: false },
      ),
      risk: "write",
      permissions: ["ticket:create"],
      requiresConfirmation: true,
      timeoutMs: 2_000,
      maxRetries: 0,
      idempotent: true,
      source: "local",
      executionMode: "sequential",
    },
    async handler({ toolCallId, args, context }) {
      return store.createTicket({
        idempotencyKey: `${context.traceId}:${toolCallId}:ticket.create`,
        tenantId: context.tenantId,
        orderId: String(args.order_id),
        title: String(args.title),
      });
    },
  };
}

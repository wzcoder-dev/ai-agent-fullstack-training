import { randomUUID } from "node:crypto";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { loadConfig } from "../config.js";
import { buildPlatform, executionContext, type Platform } from "../platform.js";
import { createGatewayModels, createGatewayProvider } from "./provider.js";

export default function managedRuntimeExtension(pi: ExtensionAPI): void {
  const initialConfig = loadConfig();
  pi.registerProvider(createGatewayProvider(initialConfig));
  pi.registerFlag("managed-user-id", { description: "业务用户 ID", type: "string", default: "trainer-1" });
  pi.registerFlag("managed-tenant-id", { description: "租户 ID", type: "string", default: "tenant_a" });
  pi.registerFlag("managed-roles", { description: "逗号分隔角色", type: "string", default: "supervisor" });
  pi.registerFlag("managed-mode", { description: "plan 或 execute", type: "string", default: "execute" });

  let platform: Platform | undefined;
  const traceId = randomUUID();

  pi.on("session_start", async (_event, ctx) => {
    platform = await buildPlatform({ connectMcp: true });
    const { model } = createGatewayModels(platform.config);
    if (!(await pi.setModel(model))) {
      throw new Error(`MODEL_UNAVAILABLE: 请设置 ${platform.config.gateway.apiKeyEnv}`);
    }
    const context = executionContext({
      traceId,
      requestId: randomUUID(),
      userId: String(pi.getFlag("managed-user-id")),
      tenantId: String(pi.getFlag("managed-tenant-id")),
      roles: String(pi.getFlag("managed-roles")).split(",").map((item) => item.trim()).filter(Boolean),
      mode: pi.getFlag("managed-mode") === "plan" ? "plan" : "execute",
    });

    for (const tool of platform.registry.snapshot(context, platform.policy)) {
      pi.registerTool({
        name: tool.modelName,
        label: tool.metadata.internalName,
        description: tool.metadata.description,
        promptSnippet: `${tool.metadata.internalName}（${tool.metadata.risk}）`,
        promptGuidelines: [`${tool.modelName} 的调用必须服从 Runtime 权限、审批与预算结果。`],
        parameters: tool.metadata.parameters,
        executionMode: tool.metadata.executionMode,
        async execute(toolCallId, params) {
          const result = await platform!.runtime.invoke({
            context,
            toolCallId,
            modelName: tool.modelName,
            args: params as Record<string, unknown>,
          });
          return {
            content: [{ type: "text", text: JSON.stringify(result) }],
            details: { governed: true, trace_id: traceId, code: result.code, approval_id: result.approvalId },
            isError: !result.ok,
          };
        },
      });
    }
    ctx.ui.notify(`Managed Runtime 已加载，trace=${traceId}`, "info");
  });

  pi.on("tool_result", (event) => {
    if (!platform || platform.runtime.wasAudited(event.toolCallId)) return;
    platform.store.addTrace({
      traceId,
      eventType: "pi-coding-agent.tool_result",
      payload: { tool_call_id: event.toolCallId, tool_name: event.toolName, is_error: event.isError },
    });
  });

  pi.on("session_shutdown", async () => {
    await platform?.close();
    platform = undefined;
  });
}

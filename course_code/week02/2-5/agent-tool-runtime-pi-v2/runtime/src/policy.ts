import type {
  ExecutionContext,
  PolicyConfig,
  PolicyDecision,
  RegisteredTool,
} from "./domain.js";

export class PolicyEngine {
  constructor(private readonly config: PolicyConfig) {}

  permissionsFor(context: ExecutionContext): Set<string> {
    return new Set(context.roles.flatMap((role) => this.config.rolePermissions[role] ?? []));
  }

  isVisible(context: ExecutionContext, internalName: string): boolean {
    return (this.config.agentToolAllowlist[context.agentName] ?? []).some((pattern) =>
      pattern.endsWith("*") ? internalName.startsWith(pattern.slice(0, -1)) : internalName === pattern,
    );
  }

  evaluate(
    context: ExecutionContext,
    tool: RegisteredTool,
    args: Record<string, unknown>,
    options: { approvalSatisfied?: boolean } = {},
  ): PolicyDecision {
    if (!this.isVisible(context, tool.metadata.internalName)) {
      return { action: "deny", code: "TOOL_NOT_VISIBLE", reason: "工具不在当前 Agent 白名单中" };
    }

    const permissions = this.permissionsFor(context);
    const missing = tool.metadata.permissions.filter((permission) => !permissions.has(permission));
    if (missing.length > 0) {
      return { action: "deny", code: "RBAC_DENIED", reason: `缺少权限: ${missing.join(", ")}` };
    }

    if (context.mode === "plan" && tool.metadata.risk !== "read") {
      return { action: "deny", code: "PLAN_MODE_DENIED", reason: "plan 模式禁止写操作" };
    }

    const orderId = args.order_id;
    if (typeof orderId === "string") {
      const allowedPrefixes = this.config.resourceTenantPrefixes[context.tenantId] ?? [];
      if (!allowedPrefixes.some((prefix) => orderId.startsWith(prefix))) {
        return { action: "deny", code: "RESOURCE_SCOPE_DENIED", reason: "目标订单不属于当前租户" };
      }
    }

    if (tool.metadata.requiresConfirmation && !options.approvalSatisfied) {
      return { action: "confirm", code: "APPROVAL_REQUIRED", reason: "该动作必须绑定参数后由人确认" };
    }

    return { action: "allow", code: "OK", reason: "策略检查通过" };
  }
}

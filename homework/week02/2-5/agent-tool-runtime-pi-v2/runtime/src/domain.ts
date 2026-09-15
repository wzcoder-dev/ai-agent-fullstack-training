import type { TSchema } from "typebox";

export type RiskLevel = "read" | "write" | "high";
export type ExecutionMode = "plan" | "execute";
export type ToolSource = "local" | "python" | "mcp";

export interface ExecutionContext {
  traceId: string;
  requestId: string;
  userId: string;
  tenantId: string;
  roles: string[];
  agentName: string;
  mode: ExecutionMode;
}

export interface ToolMetadata {
  internalName: string;
  description: string;
  version: string;
  parameters: TSchema;
  risk: RiskLevel;
  permissions: string[];
  requiresConfirmation: boolean;
  timeoutMs: number;
  maxRetries: number;
  idempotent: boolean;
  source: ToolSource;
  sensitiveFields?: string[];
  /** 部分脱敏字段（如账户号）：保留首尾字符，区别于 sensitiveFields 的完全遮蔽。 */
  maskFields?: string[];
  executionMode?: "parallel" | "sequential";
}

export interface ToolHandlerInput {
  toolCallId: string;
  args: Record<string, unknown>;
  context: ExecutionContext;
  signal: AbortSignal;
  /**
   * 由可信执行入口（CLI / 恢复流程）提供的业务幂等键。模型 Tool Args 中不存在该字段，
   * Agent 路径下由 Runtime 依据 traceId + toolCallId 派生。
   */
  idempotencyKey?: string;
}

export type ToolHandler = (input: ToolHandlerInput) => Promise<unknown>;

export interface ManagedTool {
  metadata: ToolMetadata;
  handler: ToolHandler;
  fallbackHandler?: ToolHandler;
}

export interface RegisteredTool extends ManagedTool {
  modelName: string;
}

export interface PolicyConfig {
  rolePermissions: Record<string, string[]>;
  agentToolAllowlist: Record<string, string[]>;
  resourceTenantPrefixes: Record<string, string[]>;
}

export type DecisionAction = "allow" | "deny" | "confirm";

export interface PolicyDecision {
  action: DecisionAction;
  code: string;
  reason: string;
}

export class ToolRuntimeError extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly retryable = false,
    /** 附加的结构化业务结果（如转账请求 ID、状态），会原样进入 InvocationResult.content。 */
    public readonly details?: unknown,
  ) {
    super(message);
    this.name = "ToolRuntimeError";
  }
}

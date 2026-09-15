import { validateToolArguments } from "@earendil-works/pi-ai";
import type { Tool, ToolCall } from "@earendil-works/pi-ai";
import type {
  DecisionAction,
  ExecutionContext,
  ManagedTool,
  RegisteredTool,
} from "./domain.js";
import { ToolRuntimeError } from "./domain.js";
import type { PolicyEngine } from "./policy.js";
import type { ToolRegistry } from "./registry.js";
import { approvalDigest, canonicalJson, type PlatformStore } from "./store.js";

export interface InvokeRequest {
  context: ExecutionContext;
  toolCallId: string;
  modelName: string;
  args: Record<string, unknown>;
}

export interface InvocationResult {
  ok: boolean;
  action: DecisionAction;
  code: string;
  content: unknown;
  attempts: number;
  approvalId?: string;
}

interface PreparedCall extends InvokeRequest {
  tool: RegisteredTool;
  validatedArgs: Record<string, unknown>;
}

const GENERIC_SENSITIVE_KEYS = /(?:token|secret|password|api[_-]?key|authorization)/i;

export class ToolRuntime {
  private readonly prepared = new Map<string, PreparedCall>();
  private readonly auditedCalls = new Set<string>();
  private readonly pendingApprovalByTrace = new Map<string, string>();

  constructor(
    private readonly registry: ToolRegistry,
    private readonly policy: PolicyEngine,
    private readonly store: PlatformStore,
  ) {}

  async invoke(request: InvokeRequest): Promise<InvocationResult> {
    const prepared = this.prepare(request);
    if (!("tool" in prepared)) return prepared;
    return this.executePrepared(prepared);
  }

  beforePiCall(request: InvokeRequest): { block: boolean; reason?: string } {
    const prepared = this.prepare(request);
    if ("tool" in prepared) return { block: false };
    return {
      block: true,
      reason: JSON.stringify({
        code: prepared.code,
        message: prepared.content,
        approval_id: prepared.approvalId,
      }),
    };
  }

  prepare(request: InvokeRequest): PreparedCall | InvocationResult {
    let tool: RegisteredTool;
    try {
      tool = this.registry.getByModelName(request.modelName);
    } catch {
      return this.reject(request, "TOOL_NOT_FOUND", "没有注册该工具");
    }

    let validatedArgs: Record<string, unknown>;
    try {
      validatedArgs = validateToolArguments(
        toolToPi(tool),
        callToPi(request.toolCallId, request.modelName, request.args),
      ) as Record<string, unknown>;
    } catch (error) {
      return this.reject(
        request,
        "INVALID_ARGUMENT",
        error instanceof Error ? error.message : "参数校验失败",
      );
    }

    const decision = this.policy.evaluate(request.context, tool, validatedArgs);
    if (decision.action === "deny") return this.reject(request, decision.code, decision.reason, validatedArgs);
    if (decision.action === "confirm") {
      const approval = this.store.createApproval({
        context: request.context,
        toolCallId: request.toolCallId,
        toolName: request.modelName,
        args: validatedArgs,
      });
      this.pendingApprovalByTrace.set(request.context.traceId, approval.id);
      this.store.addAudit({
        eventType: "policy_decision",
        context: request.context,
        toolCallId: request.toolCallId,
        toolName: tool.metadata.internalName,
        status: "confirm",
        code: decision.code,
        input: redact(validatedArgs, tool.metadata.sensitiveFields),
      });
      this.auditedCalls.add(request.toolCallId);
      this.store.addTrace({
        traceId: request.context.traceId,
        eventType: "tool.approval_requested",
        payload: { tool_call_id: request.toolCallId, tool_name: request.modelName, approval_id: approval.id },
      });
      return {
        ok: false,
        action: "confirm",
        code: decision.code,
        content: decision.reason,
        attempts: 0,
        approvalId: approval.id,
      };
    }

    const prepared: PreparedCall = { ...request, tool, validatedArgs };
    this.prepared.set(request.toolCallId, prepared);
    return prepared;
  }

  async executePreparedById(
    context: ExecutionContext,
    toolCallId: string,
    modelName: string,
    args: Record<string, unknown>,
  ): Promise<InvocationResult> {
    const prepared = this.prepared.get(toolCallId);
    if (!prepared) return this.invoke({ context, toolCallId, modelName, args });
    this.prepared.delete(toolCallId);
    return this.executePrepared(prepared);
  }

  async executeApproval(id: string, suppliedArgs?: Record<string, unknown>): Promise<InvocationResult> {
    const approval = this.store.getApproval(id);
    if (!approval || approval.status !== "approved") {
      return failure("APPROVAL_NOT_EXECUTABLE", "审批不存在、已过期或已被消费");
    }
    const context = JSON.parse(approval.context_json) as ExecutionContext;
    const storedArgs = JSON.parse(approval.args_json) as Record<string, unknown>;
    const args = suppliedArgs ?? storedArgs;
    const expected = approvalDigest({
      traceId: approval.trace_id,
      toolCallId: approval.tool_call_id,
      toolName: approval.tool_name,
      context,
      args,
    });
    if (expected !== approval.args_digest || canonicalJson(args) !== canonicalJson(storedArgs)) {
      return failure("APPROVAL_BINDING_MISMATCH", "工具参数已变化，旧审批失效");
    }
    if (!this.store.claimApproval(id)) return failure("APPROVAL_NOT_EXECUTABLE", "审批已被其他执行者占用");

    let tool: RegisteredTool;
    let validatedArgs: Record<string, unknown>;
    try {
      tool = this.registry.getByModelName(approval.tool_name);
      validatedArgs = validateToolArguments(
        toolToPi(tool),
        callToPi(approval.tool_call_id, approval.tool_name, args),
      ) as Record<string, unknown>;
      const policy = this.policy.evaluate(context, tool, validatedArgs, { approvalSatisfied: true });
      if (policy.action !== "allow") {
        this.store.finishApproval(id, "failed", policy.code);
        return failure(policy.code, policy.reason);
      }
    } catch (error) {
      this.store.finishApproval(id, "failed", "INVALID_ARGUMENT");
      return failure("INVALID_ARGUMENT", error instanceof Error ? error.message : "参数校验失败");
    }

    const result = await this.executePrepared({
      context,
      toolCallId: approval.tool_call_id,
      modelName: approval.tool_name,
      args,
      tool,
      validatedArgs,
    });
    this.store.finishApproval(id, result.ok ? "consumed" : "failed", result.ok ? undefined : result.code);
    return result;
  }

  pendingApproval(traceId: string): string | undefined {
    return this.pendingApprovalByTrace.get(traceId);
  }

  wasAudited(toolCallId: string): boolean {
    return this.auditedCalls.has(toolCallId);
  }

  recordPiImmediateFailure(input: {
    context: ExecutionContext;
    toolCallId: string;
    modelName: string;
    args: Record<string, unknown>;
    message: string;
  }): void {
    if (this.wasAudited(input.toolCallId)) return;
    this.reject(
      {
        context: input.context,
        toolCallId: input.toolCallId,
        modelName: input.modelName,
        args: input.args,
      },
      "PI_PREFLIGHT_REJECTED",
      input.message,
    );
  }

  private reject(
    request: InvokeRequest,
    code: string,
    reason: string,
    validatedArgs: Record<string, unknown> = request.args,
  ): InvocationResult {
    let internalName = request.modelName;
    let sensitiveFields: string[] | undefined;
    try {
      const tool = this.registry.getByModelName(request.modelName);
      internalName = tool.metadata.internalName;
      sensitiveFields = tool.metadata.sensitiveFields;
    } catch {
      // Unknown calls still need an audit record.
    }
    this.store.addAudit({
      eventType: "policy_decision",
      context: request.context,
      toolCallId: request.toolCallId,
      toolName: internalName,
      status: "deny",
      code,
      input: redact(validatedArgs, sensitiveFields),
    });
    this.auditedCalls.add(request.toolCallId);
    this.store.addTrace({
      traceId: request.context.traceId,
      eventType: "tool.denied",
      payload: { tool_call_id: request.toolCallId, tool_name: request.modelName, code },
    });
    return { ok: false, action: "deny", code, content: reason, attempts: 0 };
  }

  private async executePrepared(prepared: PreparedCall): Promise<InvocationResult> {
    const startedAt = Date.now();
    const { tool } = prepared;
    const maxAttempts = tool.metadata.idempotent ? tool.metadata.maxRetries + 1 : 1;
    let lastError: unknown;
    let attemptsUsed = 0;

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      attemptsUsed = attempt;
      const timeoutController = new AbortController();
      try {
        const output = await runWithDeadline(
          () => tool.handler({
            toolCallId: prepared.toolCallId,
            args: prepared.validatedArgs,
            context: prepared.context,
            signal: timeoutController.signal,
          }),
          tool.metadata.timeoutMs,
          timeoutController,
          // 只有“只读、幂等”的安全操作才允许在“超时”后重试：超时意味着无法确认目标
          // 操作是否已实际完成，对涉及写入的操作重试可能产生重复副作用（重复下单 / 重复扣款）。
          // 基准用 risk == read（而非仅仅 idempotent）做最终兑底：即使上游把写入误配为
          // idempotent（见 mcp adapter 按 defaultRisk 推断），写操作也绝不会因超时重试。
          tool.metadata.risk === "read",
        );
        return this.success(prepared, output, attempt, Date.now() - startedAt);
      } catch (error) {
        lastError = error;
        const retryable = lastError instanceof ToolRuntimeError && lastError.retryable;
        if (!retryable || attempt >= maxAttempts) break;
        await new Promise((resolve) => setTimeout(resolve, Math.min(25 * 2 ** (attempt - 1), 100)));
      }
    }

    if (tool.fallbackHandler && lastError instanceof ToolRuntimeError && lastError.retryable) {
      try {
        const fallbackController = new AbortController();
        const output = await runWithDeadline(
          () => tool.fallbackHandler!({
            toolCallId: prepared.toolCallId,
            args: prepared.validatedArgs,
            context: prepared.context,
            signal: fallbackController.signal,
          }),
          tool.metadata.timeoutMs,
          fallbackController,
          false, // fallback 是最后一道防线，其自身超时不应再进入重试
        );
        return this.success(prepared, output, attemptsUsed, Date.now() - startedAt, "FALLBACK_OK");
      } catch (fallbackError) {
        lastError = fallbackError;
      }
    }

    const code = lastError instanceof ToolRuntimeError ? lastError.code : "TOOL_EXECUTION_FAILED";
    const message = lastError instanceof Error ? lastError.message : "工具执行失败";
    this.store.addAudit({
      eventType: "tool_execution",
      context: prepared.context,
      toolCallId: prepared.toolCallId,
      toolName: tool.metadata.internalName,
      status: "error",
      code,
      input: redact(prepared.validatedArgs, tool.metadata.sensitiveFields),
      output: { message },
      latencyMs: Date.now() - startedAt,
    });
    this.auditedCalls.add(prepared.toolCallId);
    this.store.addTrace({
      traceId: prepared.context.traceId,
      eventType: "tool.failed",
      payload: { tool_call_id: prepared.toolCallId, tool_name: tool.modelName, code },
    });
    return { ok: false, action: "deny", code, content: message, attempts: attemptsUsed };
  }

  private success(
    prepared: PreparedCall,
    rawOutput: unknown,
    attempts: number,
    latencyMs: number,
    code = "OK",
  ): InvocationResult {
    const output = redact(rawOutput, prepared.tool.metadata.sensitiveFields);
    this.store.addAudit({
      eventType: "tool_execution",
      context: prepared.context,
      toolCallId: prepared.toolCallId,
      toolName: prepared.tool.metadata.internalName,
      status: "success",
      code,
      input: redact(prepared.validatedArgs, prepared.tool.metadata.sensitiveFields),
      output,
      latencyMs,
    });
    this.auditedCalls.add(prepared.toolCallId);
    this.store.addTrace({
      traceId: prepared.context.traceId,
      eventType: "tool.completed",
      payload: {
        tool_call_id: prepared.toolCallId,
        tool_name: prepared.modelName,
        attempts,
        latency_ms: latencyMs,
        code,
      },
    });
    return { ok: true, action: "allow", code, content: output, attempts };
  }
}

async function runWithDeadline<T>(
  work: () => Promise<T>,
  timeoutMs: number,
  controller: AbortController,
  retryOnTimeout = true,
): Promise<T> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    timeout = setTimeout(() => {
      controller.abort();
      // 超时默认不再标记为 retryable。调用方（executePrepared）是否为幂等工具决定
      // 是否允许重试：超时意味着结果不可知，重试非幂等操作会放大“幽灵副作用”风险。
      reject(new ToolRuntimeError("TOOL_TIMEOUT", "工具执行超时", retryOnTimeout));
    }, timeoutMs);
  });
  try {
    return await Promise.race([work(), deadline]);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

function toolToPi(tool: RegisteredTool): Tool {
  return {
    name: tool.modelName,
    description: tool.metadata.description,
    parameters: tool.metadata.parameters,
  };
}

function callToPi(id: string, name: string, args: Record<string, unknown>): ToolCall {
  return { type: "toolCall", id, name, arguments: structuredClone(args) };
}

function failure(code: string, message: string): InvocationResult {
  return { ok: false, action: "deny", code, content: message, attempts: 0 };
}

export function redact(value: unknown, configured: string[] = []): unknown {
  const sensitive = new Set(configured.map((item) => item.toLowerCase()));
  if (Array.isArray(value)) return value.map((item) => redact(item, configured));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [
        key,
        sensitive.has(key.toLowerCase()) || GENERIC_SENSITIVE_KEYS.test(key) ? "***" : redact(item, configured),
      ]),
    );
  }
  return value;
}

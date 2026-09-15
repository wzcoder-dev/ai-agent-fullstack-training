import { canonicalJson } from "./store.js";

export interface LoopBudgetConfig {
  maxTurns: number;
  maxToolCalls: number;
  maxRepeatedCall: number;
  maxTotalTokens: number;
  maxCostUsd: number;
  maxDurationMs: number;
}

export interface GuardDecision {
  allow: boolean;
  code: string;
  reason: string;
}

export interface StopDecision {
  stop: boolean;
  code: string;
  reason: string;
}

export class LoopGuard {
  private turns = 0;
  private toolCalls = 0;
  private totalTokens = 0;
  private costUsd = 0;
  private readonly startedAt = Date.now();
  private readonly repeatedCalls = new Map<string, number>();

  constructor(private readonly budget: LoopBudgetConfig) {}

  beforeToolCall(toolName: string, args: Record<string, unknown>): GuardDecision {
    if (Date.now() - this.startedAt >= this.budget.maxDurationMs) {
      return deny("MAX_DURATION", "Agent Run 已超过总时限");
    }
    if (this.toolCalls >= this.budget.maxToolCalls) {
      return deny("MAX_TOOL_CALLS", "工具调用预算已耗尽");
    }
    const signature = `${toolName}:${canonicalJson(args)}`;
    const count = (this.repeatedCalls.get(signature) ?? 0) + 1;
    if (count > this.budget.maxRepeatedCall) {
      return deny("REPEATED_TOOL_CALL", "检测到相同工具与参数的重复调用");
    }
    this.repeatedCalls.set(signature, count);
    this.toolCalls += 1;
    return { allow: true, code: "OK", reason: "Loop 工具预算通过" };
  }

  observeAssistantUsage(input: { totalTokens: number; costUsd: number }): void {
    this.totalTokens += input.totalTokens;
    this.costUsd += input.costUsd;
  }

  observeTurn(): void {
    this.turns += 1;
  }

  shouldStop(pendingApprovalId?: string): StopDecision {
    if (pendingApprovalId) return stop("AWAITING_APPROVAL", "工具调用正在等待人工审批");
    if (this.turns >= this.budget.maxTurns) return stop("MAX_TURNS", "Agent Loop 已达到最大轮数");
    if (this.toolCalls >= this.budget.maxToolCalls) return stop("MAX_TOOL_CALLS", "工具调用预算已耗尽");
    if (this.totalTokens >= this.budget.maxTotalTokens) return stop("MAX_TOKENS", "Token 预算已耗尽");
    if (this.costUsd >= this.budget.maxCostUsd) return stop("MAX_COST", "Cost 预算已耗尽");
    if (Date.now() - this.startedAt >= this.budget.maxDurationMs) return stop("MAX_DURATION", "Agent Run 已超过总时限");
    return { stop: false, code: "CONTINUE", reason: "预算允许继续" };
  }

  snapshot(): Record<string, number> {
    return {
      turns: this.turns,
      tool_calls: this.toolCalls,
      total_tokens: this.totalTokens,
      cost_usd: this.costUsd,
      elapsed_ms: Date.now() - this.startedAt,
    };
  }
}

function deny(code: string, reason: string): GuardDecision {
  return { allow: false, code, reason };
}

function stop(code: string, reason: string): StopDecision {
  return { stop: true, code, reason };
}

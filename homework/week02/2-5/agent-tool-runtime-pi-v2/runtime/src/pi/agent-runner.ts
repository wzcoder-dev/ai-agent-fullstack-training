import { randomUUID } from "node:crypto";
import { runAgentLoop, type AgentEvent, type AgentTool } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, Message, UserMessage } from "@earendil-works/pi-ai";
import { LoopGuard } from "../loop-guard.js";
import type { Platform } from "../platform.js";
import { executionContext } from "../platform.js";
import { createGatewayModels } from "./provider.js";
import { renderSystemPrompt } from "./prompt-client.js";
import type { TransferPayload } from "../transfer/service.js";

export interface RunAgentInput {
  prompt: string;
  userId: string;
  tenantId: string;
  roles: string[];
  agentName?: string;
  mode?: "plan" | "execute";
  onText?: (delta: string) => void;
}

export async function runManagedAgent(platform: Platform, input: RunAgentInput) {
  const traceId = randomUUID();
  const context = executionContext({
    traceId,
    requestId: randomUUID(),
    userId: input.userId,
    tenantId: input.tenantId,
    roles: input.roles,
    agentName: input.agentName,
    mode: input.mode,
  });
  const promptBundle = await renderSystemPrompt(platform.config, context);
  const { models, model } = createGatewayModels(platform.config);
  const guard = new LoopGuard(platform.config.runtime);
  let transferResult: TransferPayload | undefined;
  const tools = platform.registry.snapshot(context, platform.policy).map<AgentTool>((tool) => ({
    name: tool.modelName,
    label: tool.metadata.internalName,
    description: tool.metadata.description,
    parameters: tool.metadata.parameters,
    executionMode: tool.metadata.executionMode,
    async execute(toolCallId, args) {
      const result = await platform.runtime.executePreparedById(
        context,
        toolCallId,
        tool.modelName,
        args as Record<string, unknown>,
      );
      if (tool.metadata.internalName === "account.transfer" && typeof result.content === "object" && result.content !== null) {
        transferResult = result.content as TransferPayload;
      }
      if (!result.ok) {
        // 结构化失败要完整传给模型（含转账请求 ID/状态），便于它向用户解释，而不是只剩错误码。
        const description = typeof result.content === "string" ? result.content : JSON.stringify(result.content);
        throw new Error(`${result.code}: ${description}`);
      }
      return {
        content: [{ type: "text", text: JSON.stringify(result.content) }],
        details: { code: result.code, attempts: result.attempts, governed: true },
      };
    },
  }));
  const userMessage: UserMessage = { role: "user", content: input.prompt, timestamp: Date.now() };
  let stop = { stop: false, code: "CONTINUE", reason: "预算允许继续" };

  const messages = await runAgentLoop(
    [userMessage],
    { systemPrompt: promptBundle.prompt, messages: [], tools },
    {
      model,
      convertToLlm: (items) => items.filter(isLlmMessage),
      reasoning: undefined,
      maxTokens: platform.config.model.maxTokens,
      beforeToolCall: async ({ toolCall, args }) => {
        const guardDecision = guard.beforeToolCall(toolCall.name, args as Record<string, unknown>);
        if (!guardDecision.allow) {
          platform.runtime.recordPiImmediateFailure({
            context,
            toolCallId: toolCall.id,
            modelName: toolCall.name,
            args: args as Record<string, unknown>,
            message: `${guardDecision.code}: ${guardDecision.reason}`,
          });
          return { block: true, reason: `${guardDecision.code}: ${guardDecision.reason}` };
        }
        const decision = platform.runtime.beforePiCall({
          context,
          toolCallId: toolCall.id,
          modelName: toolCall.name,
          args: args as Record<string, unknown>,
        });
        return decision.block ? { block: true, reason: decision.reason } : undefined;
      },
      afterToolCall: async ({ result, isError }) => ({
        details: { ...(asRecord(result.details)), governed: true, trace_id: traceId },
        isError,
      }),
      shouldStopAfterTurn: async () => {
        // 确定性停止：等待人工审批（通用确认流）或转账等待审批/修改/认证时，
        // Loop 不再自主推进，下一步必须来自人工处置或用户的新指令。
        stop = guard.shouldStop(
          platform.runtime.pendingApproval(traceId),
          platform.transfers.hasBlockingTransfer(traceId),
        );
        return stop.stop;
      },
    },
    async (event) => observeEvent(platform, context, guard, event, input.onText),
    undefined,
    models.streamSimple.bind(models),
  );

  const lastAssistant = [...messages].reverse().find((message): message is AssistantMessage => message.role === "assistant");
  let finalAnswer = lastAssistant?.content
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("") ?? "";
  if (transferResult?.required_action && transferResult.transfer_request_id) {
    const id = transferResult.transfer_request_id;
    const next = {
      authenticate: `请在终端运行 npm run cli -- transfer auth ${id}，输入模拟支付密码。`,
      approve: `请人工批准或拒绝。批准命令：npm run cli -- transfer approve ${id}；批准后仍需支付认证。`,
      reject: "该请求需要人工处理。",
      modify: `请修改收款账户，例如：npm run cli -- transfer modify ${id} --to B。修改会产生新请求。`,
      query: `请查询原请求：npm run cli -- transfer status ${id}，不要重复转账。`,
    }[transferResult.required_action];
    finalAnswer = `${transferResult.message}\n请求编号：${id}\n状态：${transferResult.status}\n${next}`;
    input.onText?.(`\n${finalAnswer}`);
  }
  return {
    trace_id: traceId,
    prompt_version: promptBundle.version,
    prompt_hash: promptBundle.contentHash,
    final_answer: finalAnswer,
    stop,
    pending_approval_id: transferResult?.approval_id ?? platform.runtime.pendingApproval(traceId),
    transfer_result: transferResult,
    budget: guard.snapshot(),
    message_count: messages.length,
  };
}

async function observeEvent(
  platform: Platform,
  context: ReturnType<typeof executionContext>,
  guard: LoopGuard,
  event: AgentEvent,
  onText?: (delta: string) => void,
): Promise<void> {
  if (event.type === "message_update" && event.assistantMessageEvent.type === "text_delta") {
    onText?.(event.assistantMessageEvent.delta);
  }
  if (event.type === "message_end" && event.message.role === "assistant") {
    guard.observeAssistantUsage({
      totalTokens: event.message.usage.totalTokens,
      costUsd: event.message.usage.cost.total,
    });
  }
  if (event.type === "turn_end") guard.observeTurn();
  if (event.type === "tool_execution_end" && event.isError && !platform.runtime.wasAudited(event.toolCallId)) {
    platform.runtime.recordPiImmediateFailure({
      context,
      toolCallId: event.toolCallId,
      modelName: event.toolName,
      args: {},
      message: event.result.content
        .map((part: { type: string; text?: string }) => (part.type === "text" ? part.text ?? "" : "[image]"))
        .join("\n"),
    });
  }
  platform.store.addTrace({
    traceId: context.traceId,
    eventType: `pi.${event.type}`,
    payload: summarizeEvent(event),
  });
}

function summarizeEvent(event: AgentEvent): Record<string, unknown> {
  if (event.type === "tool_execution_start" || event.type === "tool_execution_end") {
    return { tool_call_id: event.toolCallId, tool_name: event.toolName, is_error: "isError" in event ? event.isError : false };
  }
  if (event.type === "turn_end") return { tool_result_count: event.toolResults.length };
  if (event.type === "message_end") return { role: event.message.role };
  return { type: event.type };
}

function isLlmMessage(message: any): message is Message {
  return message.role === "user" || message.role === "assistant" || message.role === "toolResult";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

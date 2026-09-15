import { randomUUID } from "node:crypto";
import { runAgentLoop, type AgentEvent, type AgentTool } from "@earendil-works/pi-agent-core";
import type { AssistantMessage, Message, UserMessage } from "@earendil-works/pi-ai";
import { LoopGuard } from "../loop-guard.js";
import type { Platform } from "../platform.js";
import { executionContext } from "../platform.js";
import { createGatewayModels } from "./provider.js";
import { renderSystemPrompt } from "./prompt-client.js";

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
      if (!result.ok) throw new Error(`${result.code}: ${String(result.content)}`);
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
        stop = guard.shouldStop(platform.runtime.pendingApproval(traceId));
        return stop.stop;
      },
    },
    async (event) => observeEvent(platform, context, guard, event, input.onText),
    undefined,
    models.streamSimple.bind(models),
  );

  const lastAssistant = [...messages].reverse().find((message): message is AssistantMessage => message.role === "assistant");
  const finalAnswer = lastAssistant?.content
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("") ?? "";
  return {
    trace_id: traceId,
    prompt_version: promptBundle.version,
    prompt_hash: promptBundle.contentHash,
    final_answer: finalAnswer,
    stop,
    pending_approval_id: platform.runtime.pendingApproval(traceId),
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

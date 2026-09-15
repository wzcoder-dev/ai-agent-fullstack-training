import { Type } from "typebox";
import { ToolRuntimeError, type ManagedTool } from "../domain.js";
import type { TransferService } from "./service.js";

export const TRANSFER_INTERNAL_NAME = "account.transfer";
export const TRANSFER_MODEL_NAME = "account__transfer";

/** JavaScript 安全整数上界（含）：2^53 - 1。 */
const MAX_SAFE_MINOR = 9007199254740991;

export interface TransferArgs {
  from_account: string;
  to_account: string;
  amount_minor: number;
  currency: string;
}

/**
 * Schema 只负责结构与类型：
 * - 账户非空且含非空白字符；
 * - amount_minor 是安全整数范围内、不含小数部分的数（>0 由 Precheck 负责）；
 * - 币种仅 CNY；
 * - 拒绝额外参数。
 * 注意：不能使用 Type.Integer —— pi-ai 校验前的 Value.Convert 会把 100.5 静默截断为 100，
 * 对金额属于篡改；multipleOf: 1 同样表达整数语义且不会被截断。
 * userId/tenantId/roles/approvalSatisfied/verified 等可信字段一概不在 Schema 中。
 */
export const transferParameters = Type.Object(
  {
    from_account: Type.String({ minLength: 1, pattern: "\\S" }),
    to_account: Type.String({ minLength: 1, pattern: "\\S" }),
    amount_minor: Type.Number({
      minimum: -MAX_SAFE_MINOR,
      maximum: MAX_SAFE_MINOR,
      multipleOf: 1,
    }),
    currency: Type.Literal("CNY"),
  },
  { additionalProperties: false },
);

export function createTransferTool(service: TransferService): ManagedTool {
  return {
    metadata: {
      internalName: TRANSFER_INTERNAL_NAME,
      description:
        "模拟转账：把整数分金额从当前用户名下的转出账户转到同租户收款账户。金额单位为分。返回结构化结果：正常账户等待支付认证，风险账户等待人工审批，收款账户不存在时等待修改。",
      version: "1.0.0",
      parameters: transferParameters,
      // 组合规则：静态 requiresConfirmation=false，避免通用策略把所有转账都送去人工审批；
      // 是否需要审批由 handler 内的收款账户风控动态决定（RISKY → WAITING_APPROVAL）。
      requiresConfirmation: false,
      risk: "high",
      permissions: ["account:transfer"],
      timeoutMs: 1_000,
      maxRetries: 0,
      idempotent: true, // 由数据库事务 + 幂等记录实际保证，不构成自动重试理由
      source: "local",
      executionMode: "sequential",
      // 账户字段部分脱敏（A → A***）：审计与结果保留可读性，又不暴露完整账号。
      maskFields: ["from_account", "to_account"],
    },
    // 注意：转账工具绝不配置 fallbackHandler —— 任何降级路径都不得再次写入资金。
    async handler({ toolCallId, args, context, idempotencyKey }) {
      const result = await service.submit({
        context,
        toolCallId,
        idempotencyKey:
          idempotencyKey ?? `transfer:${context.tenantId}:${context.userId}:${context.traceId}:${toolCallId}`,
        args: args as unknown as TransferArgs,
      });
      if (!result.ok) {
        // 失败必须是结构化结果（请求 ID、状态、错误码），不能只剩一句“转账失败”。
        throw new ToolRuntimeError(result.code, result.payload.message, false, result.payload);
      }
      return result.payload;
    },
  };
}

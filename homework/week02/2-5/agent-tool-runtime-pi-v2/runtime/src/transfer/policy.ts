import type { ExecutionContext } from "../domain.js";

/**
 * 转账专属的确定性策略检查。与 PolicyEngine 的分工：
 * - PolicyEngine（静态）：Agent 白名单、RBAC、plan 模式 —— 在 handler 之前执行；
 * - TransferPolicy（业务）：账户归属、租户范围、单笔与每日限额 —— 需要 DB 事实，
 *   在 handler 的 POLICY_CHECKING 阶段执行，并在事务内重做。
 */
export interface TransferPolicyConfig {
  currency: string;
  singleLimitMinor: number;
  dailyLimitMinor: number;
}

export interface TransferPolicyInput {
  fromAccount: { user_id: string; tenant_id: string } | undefined;
  toAccount: { tenant_id: string } | undefined;
  amountMinor: number;
}

export type TransferPolicyDecision =
  | { action: "allow"; code: "OK" }
  | { action: "deny"; code: string; reason: string };

export class TransferPolicy {
  constructor(private readonly config: TransferPolicyConfig) {}

  /**
   * 归属与限额检查。账户不存在不在此处报错（交给 Precheck 的
   * SOURCE/DESTINATION_ACCOUNT_NOT_FOUND），这里只比较“存在的账户”的归属。
   */
  evaluate(context: ExecutionContext, input: TransferPolicyInput): TransferPolicyDecision {
    if (input.fromAccount && (input.fromAccount.user_id !== context.userId || input.fromAccount.tenant_id !== context.tenantId)) {
      return { action: "deny", code: "ACCOUNT_ACCESS_DENIED", reason: "转出账户不属于当前用户" };
    }
    // Demo 约束：只允许同租户转账（README 明确声明）。
    if (input.toAccount && input.toAccount.tenant_id !== context.tenantId) {
      return { action: "deny", code: "CROSS_TENANT_DENIED", reason: "收款账户不属于当前租户（Demo 仅允许同租户转账）" };
    }
    if (input.amountMinor > this.config.singleLimitMinor) {
      return { action: "deny", code: "SINGLE_LIMIT_EXCEEDED", reason: `单笔金额超过上限 ${this.config.singleLimitMinor} 分` };
    }
    return { action: "allow", code: "OK" };
  }

  /** 每日额度单独提供：事务内外都需要结合当日已成功转出金额。 */
  dailyLimitExceeded(amountMinor: number, alreadyTransferredMinor: number): boolean {
    // 整数分加法在安全整数范围内（单笔上限与每日上限均为配置常量）。
    return alreadyTransferredMinor + amountMinor > this.config.dailyLimitMinor;
  }
}

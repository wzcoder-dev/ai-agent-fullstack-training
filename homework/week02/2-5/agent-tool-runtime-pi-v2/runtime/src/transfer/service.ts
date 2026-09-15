import { createHash, randomUUID, timingSafeEqual } from "node:crypto";
import type { ExecutionContext } from "../domain.js";
import { approvalDigest, canonicalJson, type ApprovalRecord, type PlatformStore } from "../store.js";
import type { InvocationResult, ToolRuntime } from "../runtime.js";
import { TransferTxAbort, type AccountRow, type AuthRecordRow, type TransferRequestRow, type TransferStore, type TransferTransactionRow } from "./store.js";
import type { TransferPolicy } from "./policy.js";
import { TRANSFER_MODEL_NAME, type TransferArgs } from "./tool.js";

export interface TransferConfig {
  currency: string;
  singleLimitMinor: number;
  dailyLimitMinor: number;
  authTtlSeconds: number;
  approvalTtlSeconds: number;
  /** 模拟支付认证密码。仅用于教学演示，绝不写入日志、Trace、Audit 或数据库。 */
  demoPassword: string;
}

export interface TransferPayload {
  transfer_request_id?: string;
  status: string;
  required_action?: "approve" | "reject" | "modify" | "authenticate" | "query";
  approval_id?: string;
  transaction_id?: string;
  from_account?: string;
  to_account?: string;
  amount_minor?: number;
  currency?: string;
  transfer_time?: string;
  error_code?: string;
  message: string;
  retryable?: boolean;
  superseded_by?: string;
  observability_degraded?: boolean;
}

export interface TransferResult {
  ok: boolean;
  code: string;
  payload: TransferPayload;
}

export const WAITING_STATUSES = ["WAITING_APPROVAL", "WAITING_MODIFICATION", "WAITING_AUTH"] as const;
const TERMINAL_FAILED_STATUSES = [
  "VALIDATION_FAILED",
  "POLICY_DENIED",
  "PRECHECK_FAILED",
  "REJECTED",
  "AUTH_FAILED",
  "EXECUTION_FAILED",
] as const;

export function maskAccount(accountId: string): string {
  if (accountId.length <= 2) return `${accountId}***`;
  return `${accountId.slice(0, 2)}***${accountId.slice(-2)}`;
}

export function argsDigest(args: TransferArgs): string {
  return createHash("sha256")
    .update(
      canonicalJson({
        from_account: args.from_account,
        to_account: args.to_account,
        amount_minor: args.amount_minor,
        currency: args.currency,
      }),
    )
    .digest("hex");
}

export function utcToday(): string {
  return new Date().toISOString().slice(0, 10);
}

/** 可控故障注入：仅通过环境变量由测试/演示设置，绝不暴露为模型工具参数。 */
type InjectedFault = "after_debit" | "post_commit_drop" | "post_commit_hang" | "obs_failure";

function currentFault(): InjectedFault | undefined {
  const value = process.env.TRANSFER_FAULT;
  return value === "after_debit" || value === "post_commit_drop" || value === "post_commit_hang" || value === "obs_failure"
    ? value
    : undefined;
}

/** 测试专用：事务内同步等待，用于让 BEGIN IMMEDIATE 的锁竞争可观察。 */
function syncSleep(ms: number): void {
  const shared = new SharedArrayBuffer(4);
  Atomics.wait(new Int32Array(shared), 0, 0, ms);
}

export const DEMO_ACCOUNTS: Array<Omit<AccountRow, "created_at">> = [
  { account_id: "A", user_id: "trainer-1", tenant_id: "tenant_a", currency: "CNY", balance_minor: 1_000_000, risk_level: "NORMAL" },
  { account_id: "B", user_id: "trainer-1", tenant_id: "tenant_a", currency: "CNY", balance_minor: 0, risk_level: "NORMAL" },
  { account_id: "R", user_id: "trainer-1", tenant_id: "tenant_a", currency: "CNY", balance_minor: 0, risk_level: "RISKY" },
  { account_id: "FOREIGN", user_id: "trainer-2", tenant_id: "tenant_a", currency: "CNY", balance_minor: 50_000, risk_level: "NORMAL" },
  { account_id: "CBANK", user_id: "trainer-2", tenant_id: "tenant_b", currency: "CNY", balance_minor: 0, risk_level: "NORMAL" },
];

export class TransferService {
  private runtime?: ToolRuntime;
  private observabilityDegraded = false;

  constructor(
    private readonly platformStore: PlatformStore,
    public readonly store: TransferStore,
    public readonly policy: TransferPolicy,
    private readonly config: TransferConfig,
  ) {}

  /** platform.ts 装配时注入，避免 Runtime ↔ Service 循环依赖。 */
  bindRuntime(runtime: ToolRuntime): void {
    this.runtime = runtime;
  }

  // ------------------------------------------------------------------
  // 工具入口（Agent Tool Call → Runtime → handler → 这里）
  // ------------------------------------------------------------------

  async submit(input: {
    context: ExecutionContext;
    toolCallId: string;
    idempotencyKey: string;
    args: TransferArgs;
  }): Promise<TransferResult> {
    const { context, toolCallId, idempotencyKey, args } = input;
    const digest = argsDigest(args);

    // 幂等路由：重复提交、审批恢复、认证恢复都带着原幂等键回到这里。
    const existing = this.store.getRequestByIdempotencyKey(context.tenantId, context.userId, idempotencyKey);
    if (existing) {
      if (existing.args_digest !== digest) {
        this.observeAudit(context, toolCallId, "transfer_idempotency", "error", "IDEMPOTENCY_CONFLICT", {
          transfer_request_id: existing.id,
        });
        this.observeTrace(context, "transfer.idempotent.conflict", { transfer_request_id: existing.id, tool_call_id: toolCallId });
        return {
          ok: false,
          code: "IDEMPOTENCY_CONFLICT",
          payload: {
            transfer_request_id: existing.id,
            status: existing.status,
            error_code: "IDEMPOTENCY_CONFLICT",
            message: "同一幂等键已绑定不同参数，拒绝执行",
            retryable: false,
          },
        };
      }
      return await this.routeExisting(existing, context, toolCallId);
    }

    // Agent Run 门：同一 Run 内已有转账请求（无论等待/成功/失败），不再接受新的自主转账。
    // 这是确定性服务端逻辑，不依赖提示词约束模型。
    const prior = this.store.getRequestByTrace(context.traceId);
    if (prior) {
      this.observeTrace(context, "transfer.gate.blocked", {
        transfer_request_id: prior.id,
        prior_status: prior.status,
        tool_call_id: toolCallId,
      });
      return {
        ok: false,
        code: "TRANSFER_ALREADY_REQUESTED",
        payload: {
          transfer_request_id: prior.id,
          status: prior.status,
          error_code: "TRANSFER_ALREADY_REQUESTED",
          message: `本 Run 已存在转账请求 ${prior.id}（状态 ${prior.status}）。等待人工处置或由用户发起新指令，Agent 不得自行重试或换 key 重发。`,
          retryable: false,
        },
      };
    }

    let request: TransferRequestRow;
    try {
      request = this.store.insertRequest({
        idempotencyKey,
        context,
        fromAccount: args.from_account,
        toAccount: args.to_account,
        amountMinor: args.amount_minor,
        currency: args.currency,
        argsDigest: digest,
        status: "CREATED",
      });
    } catch {
      // 并发插入命中 (tenant, user, idempotency_key) 唯一约束：另一入口刚创建了同 key 请求。
      // 约束是幂等的最终保证，这里转换为对已有请求的路由，而不是报错。
      const raced = this.store.getRequestByIdempotencyKey(context.tenantId, context.userId, idempotencyKey);
      if (raced && raced.args_digest === digest) {
        this.observeTrace(context, "transfer.idempotent.race", { transfer_request_id: raced.id, tool_call_id: toolCallId });
        return await this.routeExisting(raced, context, toolCallId);
      }
      if (raced) {
        return {
          ok: false,
          code: "IDEMPOTENCY_CONFLICT",
          payload: {
            transfer_request_id: raced.id,
            status: raced.status,
            error_code: "IDEMPOTENCY_CONFLICT",
            message: "同一幂等键已绑定不同参数，拒绝执行",
            retryable: false,
          },
        };
      }
      throw new Error("无法创建转账请求：幂等键冲突且原请求不可见");
    }
    this.observeTrace(context, "transfer.request.created", { transfer_request_id: request.id, tool_call_id: toolCallId });
    this.observeAudit(context, toolCallId, "transfer_request", "success", "OK", {
      transfer_request_id: request.id,
      from_account: maskAccount(args.from_account),
      to_account: maskAccount(args.to_account),
      amount_minor: args.amount_minor,
      currency: args.currency,
      idempotency_key_digest: createHash("sha256").update(idempotencyKey).digest("hex"),
    });
    return this.progressNewRequest(request, context, toolCallId);
  }

  hasBlockingTransfer(traceId: string): boolean {
    const request = this.store.getRequestByTrace(traceId);
    return !!request && (WAITING_STATUSES as readonly string[]).includes(request.status);
  }

  // ------------------------------------------------------------------
  // 受控入口：审批 / 拒绝 / 修改 / 认证 / 查询
  // ------------------------------------------------------------------

  async approveTransfer(identifier: string, actor: string, context: ExecutionContext): Promise<TransferResult> {
    const request = this.findRequest(identifier, context);
    if (!request) return notFound();
    const denied = this.checkControlPermission(request, context);
    if (denied) return denied;
    if (request.status !== "WAITING_APPROVAL" || !request.approval_id) {
      if (request.status === "WAITING_MODIFICATION") {
        return notExecutable(request, "收款账户不存在，只能修改后重试，不能通过审批跳过检查");
      }
      return notExecutable(request, `当前状态 ${request.status} 不可审批`);
    }
    try {
      this.store.transaction(() => {
        const fresh = this.store.getRequest(request.id);
        if (fresh?.status !== "WAITING_APPROVAL") throw new Error("Request changed");
        this.platformStore.decideApproval(request.approval_id!, "approve", actor);
        this.store.updateRequest(request.id, { status: "WAITING_AUTH", required_action: "authenticate", error_code: null, message: null });
      });
    } catch {
      const approval = this.platformStore.getApproval(request.approval_id);
      if (approval && approval.expires_at <= new Date().toISOString()) {
        this.store.transitionRequest(request.id, "WAITING_APPROVAL", { status: "REJECTED", error_code: "APPROVAL_EXPIRED", message: "审批已过期" });
        return failed("APPROVAL_EXPIRED", request, "审批已过期，请求已终止", "REJECTED");
      }
      return failed("APPROVAL_NOT_EXECUTABLE", request, "审批已被处理或不存在");
    }
    this.observeAudit(context, request.id, "transfer_approval", "success", "APPROVED", {
      decision: "approve",
      actor,
      approval_id: request.approval_id,
    });
    this.observeTrace(context, "transfer.approval.approved", { transfer_request_id: request.id, approval_id: request.approval_id });
    // approve 只解锁支付认证，绝不直接执行转账。
    return {
      ok: true,
      code: "OK",
      payload: waitingPayload(request.id, "WAITING_AUTH", "authenticate", "已批准，请完成支付认证后恢复执行", request.approval_id),
    };
  }

  async rejectTransfer(identifier: string, actor: string, context: ExecutionContext): Promise<TransferResult> {
    const request = this.findRequest(identifier, context);
    if (!request) return notFound();
    const denied = this.checkControlPermission(request, context);
    if (denied) return denied;
    if (request.status !== "WAITING_APPROVAL" && request.status !== "WAITING_MODIFICATION") {
      return notExecutable(request, `当前状态 ${request.status} 不可拒绝`);
    }
    const rejected = this.store.transaction(() => {
      if (!this.store.transitionRequest(request.id, request.status, { status: "REJECTED", required_action: null, error_code: "APPROVAL_REJECTED", message: "人工拒绝" })) return false;
      if (request.approval_id) this.store.invalidateApproval(request.approval_id, actor);
      this.store.invalidateAuthsForRequest(request.id);
      return true;
    });
    if (!rejected) return notExecutable(this.store.getRequest(request.id)!, "请求状态已改变");
    this.observeAudit(context, request.id, "transfer_approval", "success", "REJECTED", { decision: "reject", actor });
    this.observeTrace(context, "transfer.approval.rejected", { transfer_request_id: request.id });
    return {
      ok: true,
      code: "OK",
      payload: {
        transfer_request_id: request.id,
        status: "REJECTED",
        error_code: "APPROVAL_REJECTED",
        message: "已拒绝，无资金副作用，请求进入终态",
      },
    };
  }

  /**
   * 人工修改：创建全新请求（新幂等键、完整治理链路），旧请求原子置为 SUPERSEDED，
   * 旧审批与旧认证随之永久失效。
   */
  async modifyTransfer(
    identifier: string,
    changes: { to_account?: string; amount_minor?: number },
    actor: string,
    context: ExecutionContext,
  ): Promise<TransferResult | InvocationResult> {
    const request = this.findRequest(identifier, context);
    if (!request) return notFound();
    const denied = this.checkControlPermission(request, context);
    if (denied) return denied;
    const modifiable = ["WAITING_APPROVAL", "WAITING_MODIFICATION", "WAITING_AUTH"] as const;
    if (!(modifiable as readonly string[]).includes(request.status)) {
      return notExecutable(request, `当前状态 ${request.status} 不可修改`);
    }
    const newArgs: TransferArgs = {
      from_account: request.from_account,
      to_account: changes.to_account ?? request.to_account,
      amount_minor: changes.amount_minor ?? request.amount_minor,
      currency: request.currency,
    };
    if (typeof newArgs.to_account !== "string" || !/\S/.test(newArgs.to_account)) {
      return failed("INVALID_ARGUMENT", request, "修改后的收款账户不能为空");
    }
    if (!Number.isSafeInteger(newArgs.amount_minor)) {
      return failed("INVALID_ARGUMENT", request, "修改后的金额必须是安全整数");
    }

    // 原子取代：并发审批/执行在此刻失效（supersede 与执行 claim 都以状态为条件，只有一个能赢）。
    const superseded = this.store.transaction(() => {
      if (!this.store.supersedeRequest(request.id, [...modifiable])) return false;
      if (request.approval_id) this.store.invalidateApproval(request.approval_id, actor);
      this.store.invalidateAuthsForRequest(request.id);
      return true;
    });
    if (!superseded) {
      return notExecutable(request, "请求状态刚被其他操作改变，修改失败");
    }
    this.observeAudit(context, request.id, "transfer_modify", "success", "SUPERSEDED", {
      actor,
      new_args: { to_account: maskAccount(newArgs.to_account), amount_minor: newArgs.amount_minor },
    });
    this.observeTrace(context, "transfer.request.superseded", { transfer_request_id: request.id, actor });

    const runtime = this.requireRuntime();
    const invocation = await runtime.invoke({
      context,
      toolCallId: `modify-${randomUUID()}`,
      modelName: TRANSFER_MODEL_NAME,
      args: { ...newArgs },
      idempotencyKey: `transfer:modify:${context.tenantId}:${context.userId}:${request.id}`,
    });
    const payload = invocation.content as TransferPayload;
    if (payload && typeof payload === "object" && payload.transfer_request_id) {
      this.store.updateRequest(request.id, { superseded_by: payload.transfer_request_id });
    }
    return invocation;
  }

  /**
   * 模拟支付认证 + 统一恢复入口。密码只在本地内存中比较，
   * 通过后创建绑定请求/参数摘要/有效期的认证记录，再经 runtime.invoke 恢复执行。
   */
  async authenticateAndResume(
    requestId: string,
    password: string,
    context: ExecutionContext,
  ): Promise<TransferResult | InvocationResult> {
    const request = this.store.getRequest(requestId);
    if (!request || request.tenant_id !== context.tenantId || request.user_id !== context.userId) {
      return notFound();
    }
    const denied = this.checkControlPermission(request, context);
    if (denied) return denied;
    if (request.status !== "WAITING_AUTH") {
      // 已完成：幂等友好地返回原交易（并发竞争的另一个入口会走到这里）。
      if (request.status === "SUCCEEDED") {
        const tx = this.store.getTransactionByRequestId(request.id);
        return { ok: true, code: "OK", payload: successPayload(request, tx, "请求已完成，返回原交易结果，未再次扣款") };
      }
      // 终态请求透传其业务错误码，比通用的 REQUEST_NOT_EXECUTABLE 更可诊断。
      if (request.status === "REJECTED" || (TERMINAL_FAILED_STATUSES as readonly string[]).includes(request.status)) {
        return failed(
          request.error_code ?? "REQUEST_NOT_EXECUTABLE",
          request,
          `请求已处于终态 ${request.status}，不能认证或重新执行`,
        );
      }
      return notExecutable(request, `当前状态 ${request.status} 不可认证（风险账户需先人工审批）`);
    }
    this.observeAudit(context, request.id, "transfer_auth", "success", "AUTH_REQUESTED", {});
    this.observeTrace(context, "transfer.auth.requested", { transfer_request_id: request.id });

    if (!this.verifyDemoPassword(password)) {
      if (!this.store.transitionRequest(request.id, "WAITING_AUTH", { status: "AUTH_FAILED", required_action: null, error_code: "AUTH_FAILED", message: "支付认证失败" })) {
        return notExecutable(this.store.getRequest(request.id)!, "请求状态已改变");
      }
      this.observeAudit(context, request.id, "transfer_auth", "error", "AUTH_FAILED", {});
      this.observeTrace(context, "transfer.auth.failed", { transfer_request_id: request.id });
      return failed("AUTH_FAILED", request, "支付认证失败，该请求已终止", "AUTH_FAILED");
    }
    const auth = this.store.insertAuthRecord({
      requestId: request.id,
      userId: context.userId,
      tenantId: context.tenantId,
      argsDigest: request.args_digest,
      ttlSeconds: this.config.authTtlSeconds,
    });
    // 只记录认证结果与关联 ID，不记录密码或任何凭证值。
    this.observeAudit(context, request.id, "transfer_auth", "success", "AUTH_SUCCEEDED", { auth_id: auth.id });
    this.observeTrace(context, "transfer.auth.succeeded", { transfer_request_id: request.id, auth_id: auth.id });

    const resumeContext: ExecutionContext = {
      ...context,
      requestId: randomUUID(),
    };
    return this.requireRuntime().invoke({
      context: resumeContext,
      toolCallId: `auth-${randomUUID()}`,
      modelName: TRANSFER_MODEL_NAME,
      args: {
        from_account: request.from_account,
        to_account: request.to_account,
        amount_minor: request.amount_minor,
        currency: request.currency,
      },
      idempotencyKey: request.idempotency_key,
    });
  }

  /** 只读查询：按请求 ID 或幂等键，校验属主，绝不触发转账。 */
  statusQuery(identifier: string, context: ExecutionContext): TransferResult {
    const request = this.findRequest(identifier, context);
    if (!request || request.tenant_id !== context.tenantId || request.user_id !== context.userId) {
      return { ok: false, code: "REQUEST_NOT_FOUND", payload: { status: "NOT_FOUND", error_code: "REQUEST_NOT_FOUND", message: "请求不存在或无权访问", retryable: false } };
    }
    this.observeAudit(context, request.id, "transfer_status", "success", "OK", {
      transfer_request_id: request.id,
      query_digest: createHash("sha256").update(identifier).digest("hex"),
    });
    if (request.status === "SUCCEEDED") {
      const tx = this.store.getTransactionByRequestId(request.id);
      return { ok: true, code: "OK", payload: successPayload(request, tx) };
    }
    const payload: TransferPayload = {
      transfer_request_id: request.id,
      status: request.status,
      error_code: request.error_code ?? undefined,
      message: request.message ?? `当前状态 ${request.status}`,
      retryable: false,
    };
    if (request.required_action) payload.required_action = request.required_action as TransferPayload["required_action"];
    if (request.approval_id) payload.approval_id = request.approval_id;
    if (request.superseded_by) payload.superseded_by = request.superseded_by;
    return { ok: true, code: "OK", payload };
  }

  // ------------------------------------------------------------------
  // 演示数据
  // ------------------------------------------------------------------

  /** 显式初始化。默认仅在账户表为空时写入；force = true 时重置全部转账数据。 */
  seedDemoAccounts(force = false): { seeded: boolean; accounts: AccountRow[] } {
    if (!force && this.store.listAccounts().length > 0) {
      return { seeded: false, accounts: this.store.listAccounts() };
    }
    this.store.resetTransferData();
    this.store.replaceAccounts(DEMO_ACCOUNTS);
    return { seeded: true, accounts: this.store.listAccounts() };
  }

  // ------------------------------------------------------------------
  // 内部：新请求推进（Policy → Precheck → Risk）
  // ------------------------------------------------------------------

  private progressNewRequest(request: TransferRequestRow, context: ExecutionContext, toolCallId: string): TransferResult {
    const args = this.requestArgs(request);

    this.setPhase(request, "POLICY_CHECKING", context);
    const from = this.store.getAccount(args.from_account);
    const to = this.store.getAccount(args.to_account);
    const decision = this.policy.evaluate(context, {
      fromAccount: from,
      toAccount: to,
      amountMinor: args.amount_minor,
    });
    if (decision.action === "deny") {
      return this.denyNewRequest(request, context, toolCallId, "POLICY_DENIED", decision.code, decision.reason);
    }
    if (from && this.policy.dailyLimitExceeded(args.amount_minor, this.store.sumSuccessfulOutgoing(from.account_id, utcToday()))) {
      return this.denyNewRequest(request, context, toolCallId, "POLICY_DENIED", "DAILY_LIMIT_EXCEEDED", `当日累计转出加本次金额超过每日限额 ${this.config.dailyLimitMinor} 分`);
    }

    this.setPhase(request, "PRECHECKING", context);
    const precheckError = this.precheck(args, from, to);
    if (precheckError) {
      return this.denyNewRequest(request, context, toolCallId, "PRECHECK_FAILED", precheckError.code, precheckError.reason);
    }

    this.setPhase(request, "RISK_CHECKING", context);
    if (!to) {
      this.store.updateRequest(request.id, { status: "WAITING_MODIFICATION", required_action: "modify", message: "收款账户不存在" });
      this.observeAudit(context, toolCallId, "transfer_risk", "waiting", "WAITING_MODIFICATION", { transfer_request_id: request.id });
      this.observeTrace(context, "transfer.waiting", { transfer_request_id: request.id, status: "WAITING_MODIFICATION" });
      return {
        ok: true,
        code: "WAITING_MODIFICATION",
        payload: waitingPayload(request.id, "WAITING_MODIFICATION", "modify", `收款账户 ${maskAccount(args.to_account)} 不存在，请修改收款账户后重新提交`),
      };
    }
    if (to.risk_level === "RISKY") {
      const approval = this.platformStore.createApproval({
        context,
        toolCallId,
        toolName: TRANSFER_MODEL_NAME,
        args: { ...args },
        ttlSeconds: this.config.approvalTtlSeconds,
      });
      this.store.updateRequest(request.id, { status: "WAITING_APPROVAL", required_action: "approve", approval_id: approval.id, message: "等待人工审批" });
      this.observeAudit(context, toolCallId, "transfer_risk", "waiting", "WAITING_APPROVAL", { transfer_request_id: request.id, approval_id: approval.id });
      this.observeTrace(context, "transfer.approval.created", { transfer_request_id: request.id, approval_id: approval.id });
      return {
        ok: true,
        code: "WAITING_APPROVAL",
        payload: waitingPayload(request.id, "WAITING_APPROVAL", "approve", "收款账户为风险账户，等待人工审批", approval.id),
      };
    }
    this.store.updateRequest(request.id, { status: "WAITING_AUTH", required_action: "authenticate", message: "等待支付认证" });
    this.observeAudit(context, toolCallId, "transfer_risk", "waiting", "WAITING_AUTH", { transfer_request_id: request.id });
    this.observeTrace(context, "transfer.waiting", { transfer_request_id: request.id, status: "WAITING_AUTH" });
    return {
      ok: true,
      code: "WAITING_AUTH",
      payload: waitingPayload(request.id, "WAITING_AUTH", "authenticate", "请通过受控入口完成支付认证后恢复执行"),
    };
  }

  private precheck(
    args: TransferArgs,
    from: AccountRow | undefined,
    to: AccountRow | undefined,
  ): { code: string; reason: string } | undefined {
    if (!Number.isSafeInteger(args.amount_minor) || args.amount_minor <= 0) {
      return { code: "INVALID_AMOUNT", reason: "转账金额必须大于 0" };
    }
    if (args.from_account === args.to_account) {
      return { code: "SAME_ACCOUNT", reason: "转出与收款账户相同" };
    }
    if (!from) return { code: "SOURCE_ACCOUNT_NOT_FOUND", reason: `转出账户 ${maskAccount(args.from_account)} 不存在` };
    if (from.currency !== args.currency) {
      return { code: "CURRENCY_MISMATCH", reason: `转出账户币种 ${from.currency} 与转账币种 ${args.currency} 不一致` };
    }
    if (to && to.currency !== args.currency) {
      return { code: "CURRENCY_MISMATCH", reason: `收款账户币种 ${to.currency} 与转账币种 ${args.currency} 不一致` };
    }
    if (from.balance_minor < args.amount_minor) {
      return { code: "INSUFFICIENT_BALANCE", reason: `转出账户余额不足（${from.balance_minor} 分）` };
    }
    return undefined;
  }

  private denyNewRequest(
    request: TransferRequestRow,
    context: ExecutionContext,
    toolCallId: string,
    status: "POLICY_DENIED" | "PRECHECK_FAILED",
    code: string,
    reason: string,
  ): TransferResult {
    this.store.updateRequest(request.id, { status, required_action: null, error_code: code, message: reason });
    this.observeAudit(context, toolCallId, status === "POLICY_DENIED" ? "transfer_policy" : "transfer_precheck", "denied", code, {
      transfer_request_id: request.id,
      reason,
    });
    this.observeTrace(context, "transfer.phase.denied", { transfer_request_id: request.id, phase: status, code });
    return failed(code, request, reason, status);
  }

  // ------------------------------------------------------------------
  // 内部：已有请求路由
  // ------------------------------------------------------------------

  private async routeExisting(request: TransferRequestRow, context: ExecutionContext, toolCallId: string): Promise<TransferResult> {
    if (request.status === "SUCCEEDED") {
      const tx = this.store.getTransactionByRequestId(request.id);
      this.observeTrace(context, "transfer.idempotent.hit", { transfer_request_id: request.id, tool_call_id: toolCallId });
      return { ok: true, code: "OK", payload: successPayload(request, tx, "幂等命中：返回原交易结果，未再次扣款") };
    }
    const earlyPhases = ["CREATED", "VALIDATING", "POLICY_CHECKING", "PRECHECKING", "RISK_CHECKING", "EXECUTING"] as const;
    if ((earlyPhases as readonly string[]).includes(request.status)) {
      // 另一入口正在推进同一请求：返回当前状态，不进入资金执行。
      this.observeTrace(context, "transfer.duplicate_in_progress", { transfer_request_id: request.id });
      return {
        ok: true,
        code: "DUPLICATE_IN_PROGRESS",
        payload: {
          transfer_request_id: request.id,
          status: request.status,
          message: "同幂等键请求正在另一个入口处理中，稍后按请求 ID 查询结果",
        },
      };
    }
    if (request.status === "WAITING_AUTH") return await this.executeTransfer(request, context, toolCallId);
    if (request.status === "WAITING_APPROVAL" || request.status === "WAITING_MODIFICATION") {
      return {
        ok: true,
        code: request.status,
        payload: waitingPayload(request.id, request.status, request.status === "WAITING_APPROVAL" ? "approve" : "modify", `请求当前处于 ${request.status}`, request.approval_id ?? undefined),
      };
    }
    if (request.status === "SUPERSEDED") {
      return notExecutable(request, "请求已被修改取代，请使用新请求");
    }
    // 终态失败/拒绝：不因重复提交自动重新执行。
    const code = request.error_code ?? "REQUEST_NOT_EXECUTABLE";
    return failed(code, request, `请求已处于终态 ${request.status}（${code}），重复提交不会重新执行；请发起新的转账请求`);
  }

  // ------------------------------------------------------------------
  // 内部：资金执行（唯一写路径，事务内重做全部校验）
  // ------------------------------------------------------------------

  private async executeTransfer(request: TransferRequestRow, context: ExecutionContext, toolCallId: string): Promise<TransferResult> {
    const nowIso = new Date().toISOString();
    const auth = this.store.activeAuthForRequest(request.id, request.args_digest, nowIso);
    if (!auth) {
      if (this.store.hasExpiredAuthForRequest(request.id, request.args_digest, nowIso)) {
        this.observeTrace(context, "transfer.auth.expired", { transfer_request_id: request.id });
        return failed("AUTH_EXPIRED", request, "认证已过期，请重新认证后再恢复执行");
      }
      // 没有有效认证：不允许执行，返回结构化等待（不阻塞、不占用 handler 超时）。
      return {
        ok: true,
        code: "WAITING_AUTH",
        payload: waitingPayload(request.id, "WAITING_AUTH", "authenticate", "尚无有效支付认证，请先完成认证"),
      };
    }

    this.observeAudit(context, toolCallId, "transfer_execution", "success", "EXECUTION_STARTED", { transfer_request_id: request.id });
    this.observeTrace(context, "transfer.execution.started", { transfer_request_id: request.id, tool_call_id: toolCallId });

    const args = this.requestArgs(request);
    const txOps: Array<Record<string, unknown>> = [];
    const fault = currentFault();
    const txDelayMs = Number(process.env.TRANSFER_TX_DELAY_MS ?? 0);
    let transactionRow: TransferTransactionRow | undefined;

    try {
      transactionRow = this.store.transaction(() => {
        // 1. 原子占用执行权（WAITING_AUTH → EXECUTING，仅一次）。
        if (!this.store.claimRequestForExecution(request.id)) {
          throw new TransferTxAbort("CONCURRENT_STATE_CHANGED", "请求状态已被并发操作改变", request.status);
        }
        if (txDelayMs > 0) syncSleep(txDelayMs);

        const executionTime = new Date().toISOString();
        const currentAuth = this.store.activeAuthForRequest(request.id, request.args_digest, executionTime);
        if (!currentAuth) throw new TransferTxAbort("AUTH_EXPIRED", "支付认证已失效，请重新认证", "WAITING_AUTH", "authenticate");

        // 2. 审批授权仍满足（风险账户）。
        if (request.approval_id) {
          const approval = this.platformStore.getApproval(request.approval_id);
          if (!approval) throw new TransferTxAbort("APPROVAL_EXPIRED", "审批记录不存在，需重新审批", "WAITING_APPROVAL", "approve");
          if (!this.approvalMatches(request, approval)) throw new TransferTxAbort("APPROVAL_BINDING_MISMATCH", "审批与本次请求不匹配", "REJECTED");
          if (approval.status === "rejected") throw new TransferTxAbort("APPROVAL_REJECTED", "审批已被拒绝", "REJECTED");
          if (approval.status === "pending") throw new TransferTxAbort("REQUEST_NOT_EXECUTABLE", "审批尚未完成", "WAITING_APPROVAL", "approve");
          if (approval.expires_at <= executionTime) throw new TransferTxAbort("APPROVAL_EXPIRED", "审批已过期，需重新审批", "WAITING_APPROVAL", "approve");
        }

        // 3-5. 重读账户，重做归属、限额、Precheck、风险一致性检查。
        const from = this.store.getAccount(args.from_account);
        const to = this.store.getAccount(args.to_account);
        const decision = this.policy.evaluate(context, { fromAccount: from, toAccount: to, amountMinor: args.amount_minor });
        if (decision.action === "deny") throw new TransferTxAbort(decision.code, decision.reason, "POLICY_DENIED");
        if (from && this.policy.dailyLimitExceeded(args.amount_minor, this.store.sumSuccessfulOutgoing(from.account_id, utcToday()))) {
          throw new TransferTxAbort("DAILY_LIMIT_EXCEEDED", "等待期间当日额度已被占满", "POLICY_DENIED");
        }
        const precheckError = this.precheck(args, from, to);
        if (precheckError) {
          const status = precheckError.code === "DESTINATION_ACCOUNT_NOT_FOUND" ? "WAITING_MODIFICATION" : "PRECHECK_FAILED";
          throw new TransferTxAbort(precheckError.code, precheckError.reason, status, status === "WAITING_MODIFICATION" ? "modify" : undefined);
        }
        // Precheck 通过后两个账户必须都存在（precheck 内部已拒绝缺失情况）。
        if (!from || !to) {
          const missing = from ? "DESTINATION" : "SOURCE";
          throw new TransferTxAbort(
            `${missing}_ACCOUNT_NOT_FOUND`,
            `事务内${missing === "SOURCE" ? "转出" : "收款"}账户不存在`,
            from ? "WAITING_MODIFICATION" : "PRECHECK_FAILED",
            from ? "modify" : undefined,
          );
        }
        if (to.risk_level === "RISKY" && !request.approval_id) {
          throw new TransferTxAbort("RISK_ESCALATED", "等待期间收款账户升级为风险账户，需重新人工审批", "WAITING_APPROVAL", "approve");
        }
        if (!Number.isSafeInteger(from.balance_minor) || !Number.isSafeInteger(to.balance_minor)
          || to.balance_minor > Number.MAX_SAFE_INTEGER - args.amount_minor) {
          throw new TransferTxAbort("BALANCE_OVERFLOW", "余额超出安全整数范围", "PRECHECK_FAILED");
        }

        // 6. 扣款（余额守卫内置在 UPDATE 中）。
        if (this.store.debitAccount(from.account_id, args.amount_minor) !== 1) {
          throw new TransferTxAbort("INSUFFICIENT_BALANCE", "事务内余额复检失败", "PRECHECK_FAILED");
        }
        txOps.push({ op: "source.debited", account: maskAccount(from.account_id), amount_minor: args.amount_minor });

        // 故障注入：扣款后、提交前异常 → 整体 ROLLBACK。
        if (fault === "after_debit") throw new Error("INJECTED_FAULT: after_debit（已回滚）");

        // 7. 入账（账户、租户、币种一致性内置在 UPDATE 中）。
        if (this.store.creditAccount(to.account_id, args.amount_minor, context.tenantId, args.currency) !== 1) {
          const fresh = this.store.getAccount(args.to_account);
          if (!fresh) throw new TransferTxAbort("DESTINATION_ACCOUNT_NOT_FOUND", "事务内收款账户不存在", "WAITING_MODIFICATION", "modify");
          throw new TransferTxAbort("CROSS_TENANT_DENIED", "事务内收款账户租户不一致", "PRECHECK_FAILED");
        }
        txOps.push({ op: "destination.credited", account: maskAccount(to.account_id), amount_minor: args.amount_minor });

        // 8. 流水（request_id 唯一约束兜底“一请求一交易”）。
        const tx = this.store.insertTransaction(request);
        txOps.push({ op: "transaction.created", transaction_id: tx.id });

        // 9. 请求成功状态（与资金同事务提交，回滚时一起消失）。
        this.store.updateRequest(request.id, { status: "SUCCEEDED", transaction_id: tx.id, required_action: null, error_code: null, message: null });

        // 10. 原子消费认证与审批授权。
        if (!this.store.consumeAuth(currentAuth.id, new Date().toISOString())) {
          throw new TransferTxAbort("AUTH_EXPIRED", "认证已被并发消费或失效", "WAITING_AUTH", "authenticate");
        }
        if (request.approval_id && !this.store.consumeApprovedApproval(request.approval_id, new Date().toISOString())) {
          throw new TransferTxAbort("APPROVAL_EXPIRED", "审批已被并发消费", "WAITING_APPROVAL", "approve");
        }
        return tx;
      });
    } catch (error) {
      return this.store.transaction(() => this.handleTxError(request, context, error, txOps));
    }


    // 11. 已 COMMIT。观测失败绝不改写已提交事实。
    this.observeTrace(context, "transfer.tx.committed", { transfer_request_id: request.id, transaction_id: transactionRow.id, ops: txOps });
    this.observeAudit(context, toolCallId, "transfer_execution", "success", "SUCCEEDED", {
      transfer_request_id: request.id,
      transaction_id: transactionRow.id,
      amount_minor: args.amount_minor,
    });

    if (fault === "post_commit_drop") {
      this.observeTrace(context, "transfer.result.unknown", { transfer_request_id: request.id, transaction_id: transactionRow.id });
      return {
        ok: false,
        code: "OUTCOME_UNKNOWN",
        payload: {
          transfer_request_id: request.id,
          status: "OUTCOME_UNKNOWN",
          error_code: "OUTCOME_UNKNOWN",
          message: "事务已提交但响应丢失。调用方结果未知，请通过 transfer status 按原请求查询；数据库交易仍然成功。",
          retryable: false,
          observability_degraded: this.observabilityDegraded || undefined,
        },
      };
    }
    if (fault === "post_commit_hang") {
      // 模拟“提交后响应超时”：数据库已成功，调用方只能得到 TOOL_TIMEOUT，需查询确认。
      await new Promise((resolve) => setTimeout(resolve, 1_500));
    }
    return {
      ok: true,
      code: "OK",
      payload: successPayload(request, transactionRow, undefined, this.observabilityDegraded || undefined),
    };
  }

  private handleTxError(
    request: TransferRequestRow,
    context: ExecutionContext,
    error: unknown,
    txOps: Array<Record<string, unknown>>,
  ): TransferResult {
    // Recovery holds a new write transaction: do not overwrite a concurrent winner.
    const fresh = this.store.getRequest(request.id)!;
    if (fresh.status !== "WAITING_AUTH") {
      if (fresh.status === "SUCCEEDED") return { ok: true, code: "OK", payload: successPayload(fresh, this.store.getTransactionByRequestId(fresh.id)) };
      return notExecutable(fresh, "请求已被其他操作处理，请查询当前状态");
    }
    if (error instanceof TransferTxAbort) {
      if (error.code === "CONCURRENT_STATE_CHANGED") {
        const fresh = this.store.getRequest(request.id);
        if (fresh?.status === "SUCCEEDED") {
          const tx = this.store.getTransactionByRequestId(request.id);
          this.observeTrace(context, "transfer.idempotent.hit", { transfer_request_id: request.id, concurrent: true });
          return { ok: true, code: "OK", payload: successPayload(fresh, tx, "并发提交命中幂等：返回原交易结果") };
        }
        return notExecutable(fresh ?? request, "并发执行冲突，请查询当前状态");
      }
      const escalation = error.requestStatus === "WAITING_APPROVAL" && error.code !== "APPROVAL_REJECTED";
      this.store.updateRequest(request.id, { status: error.requestStatus, required_action: error.requiredAction ?? null, error_code: error.code, message: error.message });
      this.observeTrace(context, "transfer.tx.rolled_back", { transfer_request_id: request.id, reason: error.code, ops: txOps });
      this.observeAudit(context, request.id, "transfer_execution", "error", error.code, { transfer_request_id: request.id, rolled_back: true });
      if (escalation) {
        // 风险升高 / 审批过期：重新进入治理，创建新审批。
        if (request.approval_id) this.store.invalidateApproval(request.approval_id, context.userId);
        this.store.invalidateAuthsForRequest(request.id);
        const approval = this.platformStore.createApproval({
          context,
          toolCallId: `reapprove-${randomUUID()}`,
          toolName: TRANSFER_MODEL_NAME,
          args: this.requestArgs(request) as unknown as Record<string, unknown>,
          ttlSeconds: this.config.approvalTtlSeconds,
        });
        this.store.updateRequest(request.id, { approval_id: approval.id });
        this.observeTrace(context, "transfer.approval.created", { transfer_request_id: request.id, approval_id: approval.id, reapproval: true });
        return failed(error.code, { ...request, status: "WAITING_APPROVAL" }, `${error.message}；已重新创建审批 ${approval.id}`);
      }
      return failed(error.code, request, `${error.message}（事务已回滚，无资金副作用）`, error.requestStatus);
    }
    // 系统故障：已确认回滚 → EXECUTION_FAILED。
    const message = error instanceof Error ? error.message : String(error);
    this.store.updateRequest(request.id, { status: "EXECUTION_FAILED", required_action: null, error_code: "EXECUTION_FAILED", message });
    this.observeTrace(context, "transfer.tx.rolled_back", { transfer_request_id: request.id, reason: "EXECUTION_FAILED", ops: txOps });
    this.observeAudit(context, request.id, "transfer_execution", "error", "EXECUTION_FAILED", { transfer_request_id: request.id, rolled_back: true });
    return failed("EXECUTION_FAILED", request, `执行失败且已回滚：${message}`, "EXECUTION_FAILED");
  }

  // ------------------------------------------------------------------
  // 辅助
  // ------------------------------------------------------------------

  private requestArgs(request: TransferRequestRow): TransferArgs {
    return {
      from_account: request.from_account,
      to_account: request.to_account,
      amount_minor: request.amount_minor,
      currency: request.currency,
    };
  }

  private setPhase(request: TransferRequestRow, phase: string, context: ExecutionContext): void {
    this.store.updateRequest(request.id, { status: phase });
    this.observeTrace(context, "transfer.phase", { transfer_request_id: request.id, phase });
  }

  private findRequest(identifier: string, context: ExecutionContext): TransferRequestRow | undefined {
    const request = this.store.getRequest(identifier) ?? this.store.getRequestByApprovalId(identifier)
      ?? this.store.getRequestByIdempotencyKey(context.tenantId, context.userId, identifier);
    return request?.user_id === context.userId && request.tenant_id === context.tenantId ? request : undefined;
  }

  private checkControlPermission(request: TransferRequestRow, context: ExecutionContext): TransferResult | undefined {
    const decision = this.requireRuntime().transferPermission(context, this.requestArgs(request));
    if (decision.action !== "allow") return failed(decision.code, request, decision.reason);
    return undefined;
  }

  private approvalMatches(request: TransferRequestRow, approval: ApprovalRecord): boolean {
    try {
      const context = JSON.parse(approval.context_json) as ExecutionContext;
      const args = this.requestArgs(request);
      return context.userId === request.user_id && context.tenantId === request.tenant_id
        && approval.tool_name === TRANSFER_MODEL_NAME
        && canonicalJson(JSON.parse(approval.args_json)) === canonicalJson(args)
        && approval.args_digest === approvalDigest({ traceId: approval.trace_id, toolCallId: approval.tool_call_id,
          toolName: approval.tool_name, context, args: { ...args } });
    } catch { return false; }
  }

  /** The authenticated operator API may approve, but cannot authenticate or execute funds. */
  async resumeApprovedTransfer(approval: ApprovalRecord): Promise<TransferResult> {
    const request = this.store.getRequestByApprovalId(approval.id);
    if (!request) return notFound();
    const context = JSON.parse(approval.context_json) as ExecutionContext;
    const denied = this.checkControlPermission(request, context);
    if (denied) return denied;
    return this.store.transaction(() => {
      const fresh = this.store.getRequest(request.id)!;
      const current = this.platformStore.getApproval(approval.id)!;
      if (!this.approvalMatches(fresh, current)) return failed("APPROVAL_BINDING_MISMATCH", fresh, "审批参数绑定不匹配");
      if (current.status !== "approved" || current.expires_at <= new Date().toISOString()) return notExecutable(fresh, "审批不可执行或已过期");
      if (!["WAITING_APPROVAL", "WAITING_AUTH"].includes(fresh.status)) return notExecutable(fresh, "请求不可恢复");
      this.store.updateRequest(fresh.id, { status: "WAITING_AUTH", required_action: "authenticate" });
      return { ok: true, code: "WAITING_AUTH", payload: waitingPayload(fresh.id, "WAITING_AUTH", "authenticate", "审批通过，请完成支付认证", approval.id) };
    });
  }

  unknownOutcome(context: ExecutionContext, key: string): TransferPayload {
    const request = this.store.getRequestByIdempotencyKey(context.tenantId, context.userId, key);
    return { transfer_request_id: request?.id, status: "OUTCOME_UNKNOWN", error_code: "OUTCOME_UNKNOWN",
      required_action: "query", retryable: false, message: "调用超时，转账结果未知，请按原请求编号查询，禁止重新转账。" };
  }

  private verifyDemoPassword(password: string): boolean {
    const expected = createHash("sha256").update(this.config.demoPassword, "utf8").digest();
    const supplied = createHash("sha256").update(password, "utf8").digest();
    return timingSafeEqual(expected, supplied);
  }

  private requireRuntime(): ToolRuntime {
    if (!this.runtime) throw new Error("TransferService 尚未绑定 ToolRuntime");
    return this.runtime;
  }

  /**
   * 观测写入兜底：Trace/Audit 失败不允许把已提交的交易改写成失败，
   * 也不允许阻塞业务结果返回。降级事实通过 stderr 与 observability_degraded 暴露。
   */
  private observeTrace(context: ExecutionContext, eventType: string, payload: Record<string, unknown>): void {
    try {
      this.platformStore.addTrace({ traceId: context.traceId, eventType, payload });
    } catch (error) {
      this.observabilityDegraded = true;
      process.stderr.write(`[observability-degraded] trace ${eventType}: ${String(error)}\n`);
    }
  }

  private observeAudit(
    context: ExecutionContext,
    toolCallId: string,
    eventType: string,
    status: string,
    code: string,
    detail: Record<string, unknown>,
  ): void {
    try {
      this.platformStore.addAudit({
        eventType,
        context,
        toolCallId,
        toolName: "account.transfer",
        status,
        code,
        input: detail,
      });
    } catch (error) {
      this.observabilityDegraded = true;
      process.stderr.write(`[observability-degraded] audit ${eventType}: ${String(error)}\n`);
    }
  }
}

function successPayload(
  request: TransferRequestRow,
  tx: TransferTransactionRow | undefined,
  note?: string,
  degraded?: boolean,
): TransferPayload {
  return {
    transfer_request_id: request.id,
    transaction_id: tx?.id,
    status: "SUCCEEDED",
    from_account: maskAccount(request.from_account),
    to_account: maskAccount(request.to_account),
    amount_minor: request.amount_minor,
    currency: request.currency,
    transfer_time: tx?.created_at,
    message: note ?? "转账成功",
    ...(degraded ? { observability_degraded: true } : {}),
  };
}

function waitingPayload(
  requestId: string,
  status: string,
  requiredAction: NonNullable<TransferPayload["required_action"]>,
  message: string,
  approvalId?: string,
): TransferPayload {
  const payload: TransferPayload = { transfer_request_id: requestId, status, required_action: requiredAction, message };
  if (approvalId) payload.approval_id = approvalId;
  return payload;
}

function failed(
  code: string,
  request: { id: string; status?: string } | TransferRequestRow,
  message: string,
  statusOverride?: string,
): TransferResult {
  const status = statusOverride ?? ("status" in request ? request.status : undefined) ?? "FAILED";
  return {
    ok: false,
    code,
    payload: {
      transfer_request_id: request.id,
      status,
      error_code: code,
      message,
      retryable: false,
    },
  };
}

function notExecutable(request: TransferRequestRow | { id: string; status: string }, message: string): TransferResult {
  return failed("REQUEST_NOT_EXECUTABLE", request, message);
}

function notFound(): TransferResult {
  return { ok: false, code: "REQUEST_NOT_FOUND", payload: { status: "NOT_FOUND", error_code: "REQUEST_NOT_FOUND", message: "请求不存在或无权访问", retryable: false } };
}

export type { AuthRecordRow };

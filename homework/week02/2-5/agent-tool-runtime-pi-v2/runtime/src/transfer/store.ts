import { randomUUID } from "node:crypto";
import type { DatabaseSync } from "node:sqlite";
import type { ExecutionContext } from "../domain.js";

/** 模拟账户。余额、限额全部使用整数分，绝不使用浮点数。 */
export interface AccountRow {
  account_id: string;
  user_id: string;
  tenant_id: string;
  currency: string;
  balance_minor: number;
  risk_level: "NORMAL" | "RISKY";
  created_at: string;
}

export interface TransferRequestRow {
  id: string;
  idempotency_key: string;
  tenant_id: string;
  user_id: string;
  /** 首次提交时的可信执行上下文，恢复入口据此重建身份。 */
  context_json: string;
  trace_id: string;
  agent_name: string;
  from_account: string;
  to_account: string;
  amount_minor: number;
  currency: string;
  args_digest: string;
  status: string;
  required_action: string | null;
  approval_id: string | null;
  transaction_id: string | null;
  error_code: string | null;
  message: string | null;
  superseded_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface TransferTransactionRow {
  id: string;
  request_id: string;
  from_account: string;
  to_account: string;
  amount_minor: number;
  currency: string;
  /** UTC 日界线（YYYY-MM-DD），每日额度按此统计。 */
  submitted_utc_date: string;
  created_at: string;
}

/** 模拟支付认证记录。只保存“已通过”这一事实，不保存任何密码或凭证值。 */
export interface AuthRecordRow {
  id: string;
  transfer_request_id: string;
  user_id: string;
  tenant_id: string;
  args_digest: string;
  method: string;
  expires_at: string;
  consumed_at: string | null;
  created_at: string;
}

/** 业务性回滚：事务必须 ROLLBACK，但请求按明确的业务状态收敛。 */
export class TransferTxAbort extends Error {
  constructor(
    public readonly code: string,
    message: string,
    public readonly requestStatus: string,
    public readonly requiredAction?: string,
  ) {
    super(message);
    this.name = "TransferTxAbort";
  }
}

const REQUEST_COLUMNS = [
  "status",
  "required_action",
  "approval_id",
  "transaction_id",
  "error_code",
  "message",
  "superseded_by",
  "updated_at",
] as const;

export class TransferStore {
  constructor(private readonly db: DatabaseSync) {
    this.migrate();
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS accounts (
        account_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        currency TEXT NOT NULL,
        balance_minor INTEGER NOT NULL,
        risk_level TEXT NOT NULL CHECK (risk_level IN ('NORMAL', 'RISKY')),
        created_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS transfer_attempts (
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        trace_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        PRIMARY KEY (tenant_id, user_id, trace_id)
      );

      CREATE TABLE IF NOT EXISTS transfer_requests (
        id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        context_json TEXT NOT NULL,
        trace_id TEXT NOT NULL,
        agent_name TEXT NOT NULL,
        from_account TEXT NOT NULL,
        to_account TEXT NOT NULL,
        amount_minor INTEGER NOT NULL,
        currency TEXT NOT NULL,
        args_digest TEXT NOT NULL,
        status TEXT NOT NULL,
        required_action TEXT,
        approval_id TEXT,
        transaction_id TEXT,
        error_code TEXT,
        message TEXT,
        superseded_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (tenant_id, user_id, idempotency_key)
      );
      CREATE INDEX IF NOT EXISTS idx_transfer_requests_trace ON transfer_requests(trace_id);
      CREATE INDEX IF NOT EXISTS idx_transfer_requests_status ON transfer_requests(status);

      CREATE TABLE IF NOT EXISTS transfer_transactions (
        id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL UNIQUE,
        from_account TEXT NOT NULL,
        to_account TEXT NOT NULL,
        amount_minor INTEGER NOT NULL,
        currency TEXT NOT NULL,
        submitted_utc_date TEXT NOT NULL,
        created_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_tx_from_day ON transfer_transactions(from_account, submitted_utc_date);

      CREATE TABLE IF NOT EXISTS auth_records (
        id TEXT PRIMARY KEY,
        transfer_request_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        args_digest TEXT NOT NULL,
        method TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        created_at TEXT NOT NULL
      );
    `);
  }

  /**
   * 单写连接上的真实 SQLite 事务。BEGIN IMMEDIATE 立即取得写锁，
   * 跨进程并发时后到者在 busy_timeout 内等待，而不是交错写入。
   */
  transaction<T>(work: () => T): T {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const result = work();
      this.db.exec("COMMIT");
      return result;
    } catch (error) {
      try {
        this.db.exec("ROLLBACK");
      } catch {
        // 连接已失败时 ROLLBACK 自身出错，保留原始异常。
      }
      throw error;
    }
  }

  // ---- accounts ----

  getAccount(accountId: string): AccountRow | undefined {
    return this.db.prepare("SELECT * FROM accounts WHERE account_id = ?").get(accountId) as AccountRow | undefined;
  }

  listAccounts(): AccountRow[] {
    return this.db.prepare("SELECT * FROM accounts ORDER BY account_id").all() as unknown as AccountRow[];
  }

  replaceAccounts(accounts: Omit<AccountRow, "created_at">[]): void {
    this.db.exec("DELETE FROM accounts");
    const insert = this.db.prepare(
      "INSERT INTO accounts (account_id, user_id, tenant_id, currency, balance_minor, risk_level, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
    );
    const now = new Date().toISOString();
    for (const account of accounts) insert.run(account.account_id, account.user_id, account.tenant_id, account.currency, account.balance_minor, account.risk_level, now);
  }

  /** 测试与演示维护入口：直接调整余额。生产代码不得在事务外调用。 */
  setAccountBalance(accountId: string, balanceMinor: number): void {
    this.db.prepare("UPDATE accounts SET balance_minor = ? WHERE account_id = ?").run(balanceMinor, accountId);
  }

  /** 测试与演示维护入口：调整风险等级，用于验证等待期间风险变化。 */
  setAccountRisk(accountId: string, risk: "NORMAL" | "RISKY"): void {
    this.db.prepare("UPDATE accounts SET risk_level = ? WHERE account_id = ?").run(risk, accountId);
  }

  /** 事务内扣款：余额充足才生效，返回受影响行数（0 = 余额不足或账户不存在）。 */
  debitAccount(accountId: string, amountMinor: number): number {
    const result = this.db
      .prepare("UPDATE accounts SET balance_minor = balance_minor - ? WHERE account_id = ? AND balance_minor >= ?")
      .run(amountMinor, accountId, amountMinor);
    return Number(result.changes);
  }

  /** 事务内入账：收款账户必须仍存在、同租户、同币种。 */
  creditAccount(accountId: string, amountMinor: number, tenantId: string, currency: string): number {
    const result = this.db
      .prepare(
        "UPDATE accounts SET balance_minor = balance_minor + ? WHERE account_id = ? AND tenant_id = ? AND currency = ? AND balance_minor <= ?",
      )
      .run(amountMinor, accountId, tenantId, currency, Number.MAX_SAFE_INTEGER - amountMinor);
    return Number(result.changes);
  }

  // ---- transfer requests ----

  reserveAttempt(context: ExecutionContext, key: string): boolean {
    this.db.prepare("INSERT OR IGNORE INTO transfer_attempts VALUES (?, ?, ?, ?)")
      .run(context.tenantId, context.userId, context.traceId, key);
    const row = this.db.prepare("SELECT idempotency_key FROM transfer_attempts WHERE tenant_id = ? AND user_id = ? AND trace_id = ?")
      .get(context.tenantId, context.userId, context.traceId) as { idempotency_key: string };
    return row.idempotency_key === key;
  }

  getRequestByApprovalId(id: string): TransferRequestRow | undefined {
    return this.db.prepare("SELECT * FROM transfer_requests WHERE approval_id = ?").get(id) as TransferRequestRow | undefined;
  }

  insertRequest(input: {
    idempotencyKey: string;
    context: ExecutionContext;
    fromAccount: string;
    toAccount: string;
    amountMinor: number;
    currency: string;
    argsDigest: string;
    status: string;
  }): TransferRequestRow {
    const id = `TR-${randomUUID().slice(0, 8)}`;
    const now = new Date().toISOString();
    this.db
      .prepare(`
        INSERT INTO transfer_requests (
          id, idempotency_key, tenant_id, user_id, context_json, trace_id, agent_name,
          from_account, to_account, amount_minor, currency, args_digest,
          status, required_action, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
      `)
      .run(
        id,
        input.idempotencyKey,
        input.context.tenantId,
        input.context.userId,
        JSON.stringify(input.context),
        input.context.traceId,
        input.context.agentName,
        input.fromAccount,
        input.toAccount,
        input.amountMinor,
        input.currency,
        input.argsDigest,
        input.status,
        now,
        now,
      );
    return this.getRequest(id)!;
  }

  getRequest(id: string): TransferRequestRow | undefined {
    return this.db.prepare("SELECT * FROM transfer_requests WHERE id = ?").get(id) as TransferRequestRow | undefined;
  }

  getRequestByIdempotencyKey(tenantId: string, userId: string, key: string): TransferRequestRow | undefined {
    return this.db
      .prepare("SELECT * FROM transfer_requests WHERE tenant_id = ? AND user_id = ? AND idempotency_key = ?")
      .get(tenantId, userId, key) as TransferRequestRow | undefined;
  }

  countRequestsByTrace(traceId: string): number {
    const row = this.db.prepare("SELECT COUNT(*) AS total FROM transfer_requests WHERE trace_id = ?").get(traceId) as {
      total: number;
    };
    return row.total;
  }

  getRequestByTrace(traceId: string): TransferRequestRow | undefined {
    return this.db.prepare("SELECT * FROM transfer_requests WHERE trace_id = ? ORDER BY created_at DESC LIMIT 1")
      .get(traceId) as TransferRequestRow | undefined;
  }

  transitionRequest(id: string, expected: string, fields: Partial<Record<(typeof REQUEST_COLUMNS)[number], string | null>>): boolean {
    const entries = Object.entries({ ...fields, updated_at: new Date().toISOString() });
    const result = this.db.prepare(`UPDATE transfer_requests SET ${entries.map(([column]) => `${column} = ?`).join(", ")} WHERE id = ? AND status = ?`)
      .run(...entries.map(([, value]) => value), id, expected);
    return Number(result.changes) === 1;
  }

  updateRequest(id: string, fields: Partial<Record<(typeof REQUEST_COLUMNS)[number], string | null>>): void {
    const entries = Object.entries(fields).filter(([column]) =>
      (REQUEST_COLUMNS as readonly string[]).includes(column),
    );
    if (entries.length === 0) return;
    const assignments = entries.map(([column]) => `${column} = ?`).join(", ");
    const values = entries.map(([, value]) => value);
    this.db
      .prepare(`UPDATE transfer_requests SET ${assignments} WHERE id = ?`)
      .run(...values, id);
  }

  /** 事务内原子占用执行权：只有 WAITING_AUTH 的请求能进入 EXECUTING，一次性。 */
  claimRequestForExecution(id: string): boolean {
    const result = this.db
      .prepare("UPDATE transfer_requests SET status = 'EXECUTING', updated_at = ? WHERE id = ? AND status = 'WAITING_AUTH'")
      .run(new Date().toISOString(), id);
    return Number(result.changes) === 1;
  }

  /** 修改请求时原子地移出可处置状态，防止并发审批/执行。 */
  supersedeRequest(id: string, expectedStatuses: string[]): boolean {
    const placeholders = expectedStatuses.map(() => "?").join(", ");
    const result = this.db
      .prepare(`UPDATE transfer_requests SET status = 'SUPERSEDED', updated_at = ? WHERE id = ? AND status IN (${placeholders})`)
      .run(new Date().toISOString(), id, ...expectedStatuses);
    return Number(result.changes) === 1;
  }

  listRequests(status?: string): TransferRequestRow[] {
    const statement = status
      ? this.db.prepare("SELECT * FROM transfer_requests WHERE status = ? ORDER BY created_at DESC")
      : this.db.prepare("SELECT * FROM transfer_requests ORDER BY created_at DESC LIMIT 100");
    return (status ? statement.all(status) : statement.all()) as unknown as TransferRequestRow[];
  }

  // ---- transactions ----

  insertTransaction(request: TransferRequestRow): TransferTransactionRow {
    const id = `TX-${randomUUID().slice(0, 8)}`;
    const now = new Date();
    this.db
      .prepare(`
        INSERT INTO transfer_transactions (
          id, request_id, from_account, to_account, amount_minor, currency, submitted_utc_date, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      `)
      .run(
        id,
        request.id,
        request.from_account,
        request.to_account,
        request.amount_minor,
        request.currency,
        now.toISOString().slice(0, 10),
        now.toISOString(),
      );
    return this.getTransactionByRequestId(request.id)!;
  }

  getTransactionByRequestId(requestId: string): TransferTransactionRow | undefined {
    return this.db
      .prepare("SELECT * FROM transfer_transactions WHERE request_id = ?")
      .get(requestId) as TransferTransactionRow | undefined;
  }

  countTransactions(): number {
    const row = this.db.prepare("SELECT COUNT(*) AS total FROM transfer_transactions").get() as { total: number };
    return row.total;
  }

  /** 每日额度：只统计已提交成功的转出交易，日界线为 UTC 日。 */
  sumSuccessfulOutgoing(accountId: string, utcDate: string): number {
    const row = this.db
      .prepare(
        "SELECT COALESCE(SUM(amount_minor), 0) AS total FROM transfer_transactions WHERE from_account = ? AND submitted_utc_date = ?",
      )
      .get(accountId, utcDate) as { total: number };
    return Number(row.total);
  }

  // ---- auth records ----

  insertAuthRecord(input: {
    requestId: string;
    userId: string;
    tenantId: string;
    argsDigest: string;
    ttlSeconds: number;
  }): AuthRecordRow {
    const id = `AUTH-${randomUUID().slice(0, 8)}`;
    const now = new Date();
    this.db
      .prepare(`
        INSERT INTO auth_records (id, transfer_request_id, user_id, tenant_id, args_digest, method, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, 'demo-password', ?, ?)
      `)
      .run(
        id,
        input.requestId,
        input.userId,
        input.tenantId,
        input.argsDigest,
        new Date(now.getTime() + input.ttlSeconds * 1_000).toISOString(),
        now.toISOString(),
      );
    return this.db.prepare("SELECT * FROM auth_records WHERE id = ?").get(id) as unknown as AuthRecordRow;
  }

  activeAuthForRequest(requestId: string, argsDigest: string, now: string): AuthRecordRow | undefined {
    return this.db
      .prepare(
        `SELECT a.* FROM auth_records a JOIN transfer_requests r ON r.id = a.transfer_request_id
         WHERE a.transfer_request_id = ? AND a.args_digest = ? AND a.consumed_at IS NULL AND a.expires_at > ?
         AND a.user_id = r.user_id AND a.tenant_id = r.tenant_id ORDER BY a.created_at DESC LIMIT 1`,
      )
      .get(requestId, argsDigest, now) as AuthRecordRow | undefined;
  }

  hasExpiredAuthForRequest(requestId: string, argsDigest: string, now: string): boolean {
    const row = this.db
      .prepare(
        "SELECT 1 AS found FROM auth_records WHERE transfer_request_id = ? AND args_digest = ? AND consumed_at IS NULL AND expires_at <= ? LIMIT 1",
      )
      .get(requestId, argsDigest, now) as { found: number } | undefined;
    return row !== undefined;
  }

  /** 事务内一次性消费认证：并发恢复只有一个能成功。 */
  consumeAuth(id: string, now: string): boolean {
    const result = this.db
      .prepare("UPDATE auth_records SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL AND expires_at > ?")
      .run(now, id, now);
    return Number(result.changes) === 1;
  }

  /** 请求被取代时，使旧认证立即失效。 */
  invalidateAuthsForRequest(requestId: string): void {
    this.db
      .prepare("UPDATE auth_records SET consumed_at = ? WHERE transfer_request_id = ? AND consumed_at IS NULL")
      .run(new Date().toISOString(), requestId);
  }

  // ---- approvals（复用 approvals 表的补充原子操作） ----

  /** 事务内把已批准的审批直接标记为已消费（转账执行不经过 runtime.executeApproval）。 */
  consumeApprovedApproval(id: string, now: string): boolean {
    const result = this.db
      .prepare("UPDATE approvals SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'approved' AND expires_at > ?")
      .run(now, id, now);
    return Number(result.changes) === 1;
  }

  /** 请求被取代或放弃时，使 pending/approved 的旧审批永久不可用。 */
  invalidateApproval(id: string, actor: string): boolean {
    const result = this.db
      .prepare(
        "UPDATE approvals SET status = 'rejected', decided_by = ?, decided_at = ? WHERE id = ? AND status IN ('pending', 'approved')",
      )
      .run(actor, new Date().toISOString(), id);
    return Number(result.changes) === 1;
  }

  // ---- 演示数据 ----

  resetTransferData(): void {
    this.db.exec("DELETE FROM transfer_transactions; DELETE FROM auth_records; DELETE FROM transfer_requests; DELETE FROM transfer_attempts;");
  }
}

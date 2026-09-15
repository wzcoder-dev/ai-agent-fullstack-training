# 作业二：为治理框架增加转账工具

这是老师正式作业的提交目录，与 week01/llm-gateway 同样作为周作业独立存放。已有 2-5/agent-tool-runtime-pi-v2 保留为扩展实践。

## 当前状态

六项任务已实现。老师原始五项测试 **5/5 通过**（2.40 秒），补充测试 **13/13 通过**（2.07 秒）；离线演示运行成功。PermissionEngine.decide 与老师附录逐行一致，原始五项测试逐字未变。

验证环境：Python 3.13.12、Pydantic 2.13.5、pytest 9.1.1。虚拟环境已安装于本目录 .venv。

原文：[Obsidian 作业详情](</Users/zwz/notebook/obsidian/AI Agent全栈工程师训练营/week02/AI Agent 全栈工程师训练营-作业二/AI Agent 全栈工程师训练营-作业二.md>)。

## 必须采用的老师更正

原来：`amount > 80000` 时执行 `await asyncio.sleep(3.0)`。

更正后：**`amount > 40000` 时执行 `await asyncio.sleep(3.0)`，且延迟在任何余额修改之前。**

40000 不触发延迟；40000 < amount <= 50000 可进入超时路径；超过 50000 先被业务预检拒绝。不得沿用 80000。

## 实现范围

1. 按原文预置四个账户，用 (tenant_id, account_id) 为键、float 余额的 ACCOUNTS 字典。
2. TransferArgs 继承 StrictArgs；账号匹配 ^ACC-[A-Z]-[0-9]{6}$；amount 为 float，0 < amount <= 100000。
3. 预检依次检查单笔超过 50000（EXCEED_LIMIT）和余额不足（INSUFFICIENT_BALANCE），不修改余额。
4. handler 先处理超时模拟，收款账户不存在时在扣款前报 ACCOUNT_NOT_FOUND；成功更新双方余额，返回 txn_id/from/to/amount/status，status 为 accepted。
5. 注册名为 transfer 的工具；权限 transfer:execute，写操作、高风险、一次性参数绑定审批。使用 2 秒期限、max_retries=0、idempotent=False；其中 2 秒是实现选择，正文只通过测试约束为小于 3 秒。
6. 在原邮箱脱敏基础上添加账号脱敏，如 ACC-A-123456 → ACC-A-****3456。

PermissionEngine.decide 保持逐行不变；StrictArgs 的 extra="forbid" 保留；所有测试调用走 runtime.invoke()。老师五项测试原样保留。

不增加密码认证、风险账户修改、每日限额、SQLite 或真实模型依赖到正式必做流程。收款不存在时不能发生单边扣款。

## 环境与验收

Python 3.11+（基座使用 asyncio.timeout、StrEnum），Pydantic 2。正式五项测试不需要 DeepSeek 或 API Key。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
# 老师正式验收：
python -m pytest tests/test_tool_governance.py -v -k "transfer"
```

预期五项全部通过；演示中同时展示脱敏 ToolResult 和 decision/execution 审计记录。基座 AuditRecord 只存参数键名，不存账号值，账号遮罩由结果验证。

补充测试已覆盖 40000/40000.01/50000.01/100000、负数、布尔值、字符串、NaN/Infinity、收款不存在、跨租户收付款以及原邮箱和密钥脱敏。老师测试覆盖 50000 超时、审批参数篡改与重放等。

```bash
# 全部 18 项测试
python -m pytest tests -v
# 离线演示：包含转账确认、模拟审批、成功结果与审计打印
python tool_governance_demo.py
```

离线演示由受信任代码模拟人工批准。可观察到 confirm/APPROVAL_REQUIRED，随后 accepted、ACC-A-****3456、ACC-A-****4321，以及 decision/APPROVED 和 execution/OK 审计。

## 调用链与实现边界

ToolCall → Runtime 查找工具 → Pydantic 校验 → PermissionEngine（deny、plan、白名单、RBAC、业务预检、一次性参数绑定审批等原有优先级）→ handler → Runtime 脱敏和审计 → ToolResult。未通过检查时不进入 handler；待确认时没有资金变更。

老师五项测试走同一 runtime.invoke；基座对非幂等写操作不自动重试，超时返回 TIMEOUT_UNKNOWN。本 Demo 在可取消的 sleep 后才扣款，所以超时测试可以额外证明余额未变；该证明不能推广到任意外部支付系统。余额使用题目指定的浮点内存字典，进程退出即丢失，不提供持久化事务或资金幂等。

原 --agent 入口保留为可选订单查询示例，需要另装 openai 和配置 DeepSeek；它不是转账演示入口，本次未运行。正式转账使用上述离线演示与测试完成验收。

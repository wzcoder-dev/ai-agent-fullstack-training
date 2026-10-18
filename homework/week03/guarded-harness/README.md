# 第三周作业：Guarded Coding Harness

作业原文：https://geekbang.feishu.cn/docx/SvsPd37Zpo6v1ExX73Dc54H5nSf

在不修改 smolagents 核心的前提下，为 `ToolCallingAgent` 实现门禁、审批、执行边界、状态与计划、Checkpoint 恢复、审计和完成验收，支持代码理解与测试修复。

已验证：87 项测试通过、38 项清单复验通过、5 条跨进程演示符合预期；DeepSeek 真实修复最终为 `COMPLETED / VERIFIED`。最新确定性与真实模型记录分别由 [artifacts/latest.json](artifacts/latest.json) 和 [artifacts/live-latest.json](artifacts/live-latest.json) 指向。

精简学习手册见 [learning-guide.html](learning-guide.html)：9 个设计点、面向 C 开发者的中文注释核心代码、每点手动复测及最终掌握清单。可直接用浏览器离线阅读；原始验收记录保留在 artifacts/，不在学习页面展开。

## 安装与验证

以下从克隆后的仓库根目录执行：

```bash
cd homework/week03/guarded-harness
uv sync --locked
uv run pytest -q
uv run python scripts/verify.py
uv run python scripts/build_guide.py
```

使用 Python 3.12，smolagents 1.26.0；uv.lock 锁定依赖。Fake Model 不需要 API Key，运行循环使用实际 ToolCallingAgent。

## 启动示例与审批恢复

每次新演示请使用一个新目录，以免覆盖已有工作区。下列 demo01 可以替换为 demo02。

```bash
uv run python -m guarded_harness.cli init-demo --workspace runs/demo01/repo
uv run python -m guarded_harness.cli run --workspace runs/demo01/repo --run-dir runs/demo01/state --kind repair
# 观察 WAITING_APPROVAL、call-4 和待审批 Diff，此时 calc.py 仍未修改。
uv run python -m guarded_harness.cli approve --workspace runs/demo01/repo --run-dir runs/demo01/state --call-id call-4 --actor learner
uv run python -m guarded_harness.cli resume --workspace runs/demo01/repo --run-dir runs/demo01/state
uv run python -m guarded_harness.cli status --workspace runs/demo01/repo --run-dir runs/demo01/state
```

理解任务改用 `--kind understand`，其待审批报告调用为 `call-3`。拒绝审批使用 `approve ... --deny`。

## 目录

- `guarded_harness/`：Gate、Sandbox、状态、Checkpoint、Audit、Harness、模型适配、CLI 和演示。
- `tests/`：老师要求的六个测试文件、课程对齐回归测试与共享 fixture。
- `scripts/verify.py`：运行自动化测试与五个跨进程示例，保存真实输出。
- `scripts/crash_demo.py`：仅对自带示例注入进程崩溃。
- `scripts/build_guide.py`：从当前源码生成带中文注释的精简 HTML，不依赖验收产物。
- `artifacts/`：测试结果、JUnit、完整命令记录、审计、Checkpoint 与 Diff。
- `runs/`：本地演示工作区，已忽略；状态目录必须位于被操作仓库之外。

## 执行边界

本项目是教学用应用层 Sandbox，不提供容器或内核隔离。只对可信示例/可信测试仓库运行；pytest 会执行仓库中的 Python 代码，其能力仍等同当前用户。路径验证、命令白名单、最小环境、输出限制与进程组终止均有测试；要处理不可信仓库应额外接容器/VM 与网络隔离。

CLI 在单个 run 内使用文件锁；不要在多个进程中直接调用 Python 库绕过 CLI。审计日志只追加并带哈希链，可发现已有行被改写，但没有外部签名/锚点，不能抵抗有文件权限的人重写整条链。

`UNKNOWN_RESULT` 与 `NEEDS_REVIEW` 都需要人工核对，不提供自动清除锁定状态的捷径；确认后可在新 run 中继续。审批是本机显式操作，不是多用户身份认证系统。

## 可选真实模型

安装 `uv sync --locked --extra live`，在本机环境设置 `OPENAI_API_KEY`、`MODEL_ID`，兼容服务可设置 `OPENAI_BASE_URL`；不要把密钥写进仓库。使用新演示目录，run/resume 增加 `--model openai`。真实调用会把任务、工具定义及工具输出发送到配置的服务，确认该服务和示例代码适合后再手动运行。已使用 DeepSeek 的 deepseek-flash（非思考模式）完成真实测试修复与跨进程恢复，T2 证据由 artifacts/live-latest.json 指向；完整记录保留在 artifacts/。


## DeepSeek 真实验收

当前环境已设置 `DEEPSEEK_API_KEY` 时，无须映射到 OPENAI_API_KEY。CLI 的 run/resume 使用 `--model deepseek`；默认 `DEEPSEEK_MODEL=deepseek-flash`，端点固定为 https://api.deepseek.com。

```bash
uv sync --locked --extra live
uv run --extra live python scripts/verify_deepseek.py
uv run python scripts/build_guide.py
```

验收脚本仅对自带示例中与 FIXED fixture 完全相同的 calc.py 修复自动审批，actor 明确记录为测试脚本。其他模型写入不自动批准。每次执行会产生真实 API 请求和新的 artifacts/deepseek-时间戳/ 证据。模型 ID、请求 ID、耗时和 token 用量写入审计，密钥不落盘。DeepSeek 当前默认思考模式与框架 tool_choice=required 不兼容，本次显式使用 thinking=disabled；未验证思考模式。

## 课程对齐增强（R1–R6）

借鉴课程 `3-5/harness_agent` 的完成契约、PlanStep、状态迁移与恢复检查，以及 `3-7/project3/v2` 的上下文/预算机制，保留 Python + smolagents。

- **R1**：同一范围只看最后一次测试尝试，失败、取消、超时与未知阻止旧通过。
- **R2**：理解与修复分别使用独立完成契约，返回全部缺项；理解报告固定为 `artifacts/report.md`。
- **R3**：状态由 `transition` 统一迁移，校验合法性并跨进程保留历史序号。
- **R4**：结构化步骤记录目标、依赖、状态、尝试与证据；先读源码再修改，改后重新验证。
- **R5**：默认总 token 50000、有效运行时限 300 秒、上下文 24000 字符、近期步骤 2；旧步骤按规则摘要，完整事实不删。预算在模型请求和工具调用边界检查；真实 usage 回来后才能统计本次开销，单个已发请求可能超过 token 阈值，因此不是费用预扣硬上限。
- **R6**：v2 Checkpoint 增加 checksum 与结构检查。`inspect` / `status` 只读报告；旧 v1 记录只读保留，不自动迁移续跑。用新目录演示新版。

```bash
# 不发起新模型请求：核对当前源码的最新 DeepSeek 证据，执行其余全部清单并更新 HTML。
uv run --extra live python scripts/verify_checklist.py
# 明确包含新一轮 DeepSeek 请求：
uv run --extra live python scripts/verify_checklist.py --live
# 分组验证新增六项：
uv run pytest -q tests/test_course_alignment.py
# 新版只读恢复检查：
uv run python -m guarded_harness.cli inspect --workspace runs/demo01/repo --run-dir runs/demo01/state
```

`run` 可设置 `--max-tokens`、`--max-duration`、`--context-max-chars`、`--keep-recent`；`resume` 延续原存档预算。暂停等待人工的时间不计入有效运行时间。Checkpoint 的哈希不是防恶意重签的签名机制。

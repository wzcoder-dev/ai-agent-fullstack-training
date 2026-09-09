# 1-1 作业：Mini Coding Agent

**任务**：输入一个代码问题 → 通过 Agent Loop 输出一个**可校验**的修改计划。

对应课件要求的三块：输入（问题描述 + 仓库摘要 + 相关文件片段）、输出契约（action / file_path / reason / risk）、异常与切换（429/超时/拒答、Schema 校验失败、模型可替换）。

## 快速开始

```bash
cd homework/week01/1-1
uv sync --extra dev

# 1) 离线回归评测（不需要任何 API key，模型响应由用例脚本化）
uv run python eval_runner.py --provider fake

# 2) 单元测试（同样离线）
uv run pytest -q

# 3) 真实模型运行
export DEEPSEEK_API_KEY=sk-xxx
uv run python main.py --issue "run_python_in_sandbox 没有超时保护，子进程可能卡死，请规划修复"

# 4) 切换到智谱 GLM（Loop 不变，只换 Adapter）
export ZHIPU_API_KEY=xxx
uv run python main.py --issue "..." --provider zhipu            # 默认 GLM-5.3-Flash
uv run python main.py --issue "..." --provider zhipu --model GLM-5.3-Flash   # 或显式指定模型名
```

## 密钥配置（填哪里）

两种方式任选其一：

- **方式 A（推荐）**：把 `.env.example` 复制为 `.env`，在等号后填入密钥：

  ```bash
  cd homework/week01/1-1
  cp .env.example .env
  # 编辑 .env：ZHIPU_API_KEY=你的密钥（或 DEEPSEEK_API_KEY=...，只填要用的即可）
  ```

  `.env` 已被 `.gitignore` 忽略，不会提交；启动时自动加载，且不覆盖已 export 的环境变量。

- **方式 B**：直接在 shell 里 `export ZHIPU_API_KEY=xxx`（或 `DEEPSEEK_API_KEY=...`）。

## 架构

```
issue ──▶ context.py ──┐
                       │   system prompt(仓库摘要+工具协议+输出契约)
                       ▼
                 ┌───────────────┐   tool_call JSON   ┌──────────────┐
   issue ──────▶ │   Agent Loop  │ ◀────────────────▶ │   ToolHub    │
                 │   (loop.py)   │    [toolResult]    │ list_files   │
                 └──────┬────────┘                    │ read_file    │
                        │ 计划 JSON                    └──────────────┘
                        ▼
                 schema.py 契约校验 ──失败──▶ 字段错误回传，重试(≤2次)
                        │ 通过
                        ▼
                   PlanContract + trace.py 全程留痕(JSON)
```

| 模块 | 职责 |
| --- | --- |
| `plan_agent/schema.py` | 输出契约：`action ∈ {edit, ask_user, finish}`、`file_path`（必须仓库内相对路径，禁绝对路径/`..`/反斜杠）、`reason` 1-500 字、`risk ∈ {low, medium, high}`；校验失败生成「具体到字段」的错误文本 |
| `plan_agent/context.py` | 输入三要素：`scan_repo` 自动生成仓库摘要（目录树 + pyproject 依赖 + 现有测试）；`ToolHub` 提供只读工具 `list_files` / `read_file`，带路径安全（resolve 后比较，符号链接逃逸也拦截）与字符预算（单次 6k / 全程 24k，约等于 token 预算） |
| `plan_agent/adapters.py` | `ModelAdapter` 协议（`complete(messages) -> str`）+ `DeepSeekAdapter` / `ZhipuAdapter`（智谱 OpenAI 兼容端点，预留）+ `FakeAdapter`（脚本化响应，离线测试） |
| `plan_agent/loop.py` | Agent Loop：调模型 → 识别工具请求/计划输出 → 执行工具或契约校验 → 失败回传重试 → 停止条件 |
| `plan_agent/trace.py` | 运行留痕：每次 LLM 调用/错误/重试/工具执行/Schema 失败/最终结论，落 JSON |
| `main.py` | CLI：`--issue/--issue-file --repo --provider --model --trace-out` |
| `eval_runner.py` + `cases/` | 回归评测：3 个用例（edit / ask_user / finish），结构化期望判定 + pass rate |

## 异常与停止条件（全部写进 trace，不静默、不死循环）

| 异常 | 处理 |
| --- | --- |
| 429 / 超时 / 连接失败 | 指数退避重试（1s、2s），最多 2 次；耗尽后 `failed` 返回，trace 记录 `llm_error` + `llm_retry` |
| 拒答（content_filter） | 不重试，立即 `failed`，trace 记录 |
| Schema 校验失败 | 把字段级错误（如 `- risk: Field required`）回传给模型重试，最多 2 次；耗尽后 `failed`（kind=`schema_retries_exhausted`） |
| 工具轮次超限 | 默认 6 轮，超限 `failed`（kind=`max_tool_rounds`） |
| 工具执行失败（路径越界、文件不存在） | 不终止 Loop，错误作为 `[toolResult]` 回填，模型可自行纠正 |

## 验收四条如何落地

| 验收项 | 落地方式 |
| --- | --- |
| 同类请求可回归 | `eval_runner.py`：用例 = issue + 结构化期望（action / file_path_contains / reason_contains / risk_in），输出 pass rate；`--provider fake` 全离线可跑（当前 3/3） |
| 失败可定位 | 每次 run 产出一个 trace JSON（`traces/`），时间线含 `run_start → llm_response → tool_start → tool_end → schema_error/llm_error → plan/run_end`；eval 失败用例自动保存并打印路径。示例见 `traces/demo-trace.json` |
| 模型可替换 | Loop 只依赖 `ModelAdapter` 协议；39 个单测全部用 `FakeAdapter` 跑通即证明 Loop 与具体模型解耦；新增供应商 = 加一个 Adapter 子类（智谱已按此预留，声明 base_url/默认模型/环境变量名即可） |
| 输出可校验 | pydantic 契约强校验 + eval 双重判定；模型输出多带无害字段会被忽略，缺字段/错枚举/路径越界/超长 reason 都会被字段级报错拦下 |

## 关键设计决策

1. **Loop 带只读工具**（已确认）：`list_files` / `read_file` 让模型自己收集上下文，「相关文件片段不超过 token 预算」由 ToolHub 的字符预算强制执行——超出后工具直接提示模型基于已有信息收尾。
2. **GLM 预留**（已确认）：`ZhipuAdapter` 走智谱 OpenAI 兼容端点 `https://open.bigmodel.cn/api/paas/v4/`，密钥读 `ZHIPU_API_KEY`；默认模型 `GLM-5.3-Flash`，可用 `--model` 覆盖。
3. **默认模型 `deepseek-v4-flash`**（已确认，沿用课程代码），`--model` 可覆盖。
4. **默认被分析仓库是 `course_code/week01/1-1`**（已确认，从 git 根定位），eval 用例的 `file_path` 期望都以它为基准。
5. **提示词约定 JSON + 代码校验**，不依赖 `response_format`：本节重点是 Loop 与契约本身，结构化输出是 1-5 的主题。
6. **同步实现**：工具是纯本地只读 I/O，没有需要并发的点；同步让 Loop 与测试更直白。流式（1-3）与沙箱执行是后续小节主题。
7. **契约外字段忽略**：模型多输出一个 `summary` 不触发重试；缺字段/错枚举/路径非法/超长 reason 才会。

## 运行结果

- `uv run pytest -q` → **39 passed**
- `uv run ruff check .` → **All checks passed!**
- `uv run python eval_runner.py --provider fake` → **Pass rate: 3/3**

真实供应商（DeepSeek / 智谱）的评测命令同上，配好 key 后运行；trace 会落在 `traces/` 下。

## 已知边界（留给后续小节）

- 字符预算是 token 预算的近似（≈1 token 3~4 字符英文 / 1~1.5 字符中文），精确计费等接 Gateway 后再做；
- 单文件计划：契约只表达「改哪个文件 + 为什么」，多文件计划与真正执行 edit 是后续小节的事；
- `reason_contains` 这类关键词期望对真实模型偏宽松，回归主要用于防「契约回归」（还能不能输出合法计划）而非语义回归。

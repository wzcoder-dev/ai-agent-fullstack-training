# 第一题：pi 项目长期记忆

独立 pi 扩展，自行实现项目长期记忆，使用 OKF v0.2 文件组织格式；不需要安装现成 OKF 记忆插件，也不修改 pi 核心。

已完成类型检查、13 项自动检查、老师三组原始提示词的真实模型验证，以及用户手动操作的三张截图。按用户要求，交付目录仅保留源码、测试、依赖清单、运行说明和最终截图；依赖环境、临时项目、会话、日志、导出页面与旧截图已清理。

## 最终截图

以下均为用户实际手动验证时截取的原始图片，未重新绘制内容。

| 题目要求 | 截图 | 展示内容 |
| --- | --- | --- |
| 建库结果 | [01-create.png](artifacts/screenshots/01-create.png) | 记忆创建成功、实际保存路径、测试入口、命令、来源和实跑结果 |
| 查询对比与回答 | [02-new-session.png](artifacts/screenshots/02-new-session.png) | 记忆来源与当前文件逐项核对，说明本次没有重跑测试 |
| 更新后的回答 | [03-update.png](artifacts/screenshots/03-update.png) | 测试迁移、说明更新、新命令 5 passed、同一条记忆更新及新来源 |

截图按老师确认的结果展示口径整理，不要求画面包含工具调用。

## 目录

```text
src/                         # pi 扩展与记忆存储实现
fixtures/login-project/      # 干净的登录会话源码、测试和测试说明
scripts/                     # 验收脚本与手动启动器模板源码
tests/                       # CRUD、项目隔离、跨进程与启动器回归检查
artifacts/screenshots/       # 最终三张手动截图
package.json
package-lock.json
tsconfig.json
```

`fixtures/` 是可复现的测试样本源码，不是临时运行目录。样本初始入口保持 `tests/test_session.py`，用于重新演示迁移。

## 重建环境

本次验证版本为 pi 0.87.1、Node 25.8.0、Python 3.14.3、pytest 8.4.2；Node 要求 >=22.19.0。依赖采用精确版本及 npm lockfile。

在本目录执行：

```sh
npm ci --ignore-scripts
uv venv .venv
uv pip install --python .venv/bin/python -r fixtures/login-project/requirements.txt
npm run check
npm test
```

`@earendil-works/pi-coding-agent` 为开发依赖，用于本地 CLI 与类型检查；不是 OKF 插件。真实模型验证需要 pi 已配置可用模型，本次使用 DeepSeek / deepseek-flash。代码和截图不包含密钥。

## 手动验证

先重建依赖，再在本目录准备新的样本副本：

```sh
PM_ROOT="$PWD"
mkdir -p runs
PM_DEMO="$(mktemp -d "$PM_ROOT/runs/manual-XXXXXX")"
cp -R "$PM_ROOT/fixtures/login-project/." "$PM_DEMO/"
export PATH="$PM_ROOT/.venv/bin:$PATH"
cd "$PM_DEMO"
"$PM_ROOT/node_modules/.bin/pi" --no-extensions -e "$PM_ROOT/src/extension.ts"
```

启动 pi 的工作目录就是目标项目根目录，不向上寻找外层 Git 根。`--no-extensions` 关闭其他扩展自动发现，显式 `-e` 仍加载本作业扩展。

依次输入老师原始提示词：

```text
读取当前项目的会话测试文件和测试说明，将测试路径和运行命令保存到项目长期记忆，附上来源。返回实际保存路径。
```

首次完成后，空输入框 Ctrl+D 退出 pi，再在同一目标项目目录执行上述 pi 启动命令。不要传继续或恢复会话参数。全新会话输入：

```text
从当前项目长期记忆中查找会话测试入口，再核对当前文件是否存在。
```

然后输入第三步：

```text
将会话测试迁移到 tests/auth/test_session.py，同步修改测试说明，运行新测试命令。根据实际结果更新原来的长期记忆。
```

预期：初始记忆保存入口与命令、来源及实际路径；新会话找到记录并核对文件；迁移后执行 `python -m pytest tests/auth/test_session.py -q` 得到 5 passed，并更新原标识及来源，不新增替代记录。正文将旧路径标为迁移历史不违反题面，不额外要求旧路径字符串全文搜索必须为空。

旧临时启动入口已随环境清理。如需复用 `scripts/manual-launcher.zsh`，将其复制到 `runs/<本次目录>/start-pi.command`，并在该目录下准备 `login-project/`；它会解析符号链接位置，定位项目并传入扩展参数。

## 工具契约

| 工具 | 行为 |
| --- | --- |
| `project_memory_create` | id + type/title/description/body/sources；创建记录，已有则报错 |
| `project_memory_search` | 当前项目内搜标题、描述、正文；空格分词按 AND 匹配，limit 1–50，默认 10 |
| `project_memory_read` | 按标识返回完整记录、来源和实际路径 |
| `project_memory_update` | 按原标识完整替换当前正文和来源；不存在则报错，不隐式新增 |
| `project_memory_delete` | 删除记录文件、同步索引、记录删除动作；不存在则报错 |

稳定标识为记忆文件相对路径，不含 `.md`，例如 `testing/session-entry`；不等于正文中的业务测试路径。ID 使用字母、数字、下划线、短横线和 `/`，index/log 为保留名。

创建与更新的 type/title/description/body 不得为空，前三项为单行；正文最多 100000 字符。sources 为 1–20 项 `{path, title?}`，path 必须是当前存在的项目内相对文件路径。保存为 OKF `sources[].resource` 的 file URL，同时保留项目相对来源。来源文件存在不等于内容已被核实，需要以实际读取和运行结果为准。

pi 负责读项目文件、迁移、修改说明及执行测试，记忆工具只校验并保存内容，不能把写入成功当作测试成功。

## 存储与边界

```text
<pi 工作目录>/.pi/project-memory/
├── index.md
├── log.md
└── testing/session-entry.md
```

按照 [官方 OKF v0.2 SPEC](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) 处理概念标识、type、sources 和 generated；index/log 为官方可选文件，本项目主动维护。title、description、body、sources 是本项目业务约束。读取容忍未知类型和字段，更新保留未知顶层元信息。

路径拒绝目录穿越、保留名与符号链接。写入采用排他锁和逐文件原子替换，普通 I/O 失败尝试恢复记录、索引与日志；不承诺进程崩溃时的跨文件事务。只读操作不获取写锁，多进程同时读写时应待写入完成后复查。异常退出留下 `.write.lock` 时，先确认无写进程再手动移除。

## 自动验收

```sh
npm run verify:teacher
```

该脚本使用老师三条原文，启动三个独立 pi 进程，不向提示词补充工具名或记忆 ID；检查落盘、全新会话回读、迁移、新命令、原记录更新及来源，并独立复跑测试。

```sh
npm run verify
```

该脚本保留早期带补充指令的工程验收，并额外验证删除。两者都调用真实模型，可用 `MEMORY_VERIFY_PROVIDER`、`MEMORY_VERIFY_MODEL` 选择已配置模型。运行会重新生成 `runs/` 和 `artifacts/` 下的会话、快照及结果，当前交付中这些旧产物已清理。

学习分析、设计讨论与复盘位于 Obsidian 的 `week04/实践记录/作业四/`，不放入代码交付目录。

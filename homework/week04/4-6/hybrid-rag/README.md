# 第二题：LlamaIndex 混合检索 RAG

Python 命令行实现：同一批节点建立向量与 BM25 索引，使用真实 Embedding；分别展示向量、BM25、加权 RRF 前三条，并用真实模型生成来源引用回答。资料更新为 20 分钟后同步重建两路索引，旧节点退出当前检索。

已完成真实本地 BGE 与 DeepSeek 验收，12 项检查通过。交付保留源码、测试、完整资料副本、依赖清单、最终运行记录和三张截图。虚拟环境、模型缓存、索引及中间实验产物已清理；历史实验记录归档到 Obsidian 的 week04/实践记录/作业四/。

## 最终截图与证据

| 阶段 | 截图 | 运行记录 |
| --- | --- | --- |
| 建立双路索引 | [01-build.jpg](artifacts/screenshots/01-build.jpg) | [建库结果](artifacts/logs/acceptance/01-build-30.json) |
| 三路查询与引用回答 | [02-query.jpg](artifacts/screenshots/02-query.jpg) | [30 分钟回答](artifacts/logs/acceptance/02-query-30.json) |
| 更新后重新查询 | [03-update.jpg](artifacts/screenshots/03-update.jpg) | [20 分钟回答](artifacts/logs/acceptance/03-query-20.json) |

截图页面直接展示真实 JSON 运行证据。完整验收结论见 [summary.json](artifacts/logs/acceptance/summary.json)，八题结果见 [04-eight-questions.json](artifacts/logs/acceptance/04-eight-questions.json)，自动检查输出见 [pytest.txt](artifacts/logs/pytest.txt)。

## 重建环境与运行

Python 3.11+，本次验证使用 3.14。在本目录执行：

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python download_bge.py
export BGE_BACKEND=torch
.venv/bin/python -m hybrid_rag build --model bge
.venv/bin/python -m hybrid_rag query --model bge
.venv/bin/python -m pytest -q
```

下载器从同名 BGE 官方 ModelScope 镜像获取 Safetensors 与 tokenizer，逐文件核对 SHA256，只加载本地数据文件，不执行远程模型代码。模型缓存写到忽略的 `.model-cache/`。本次采用 torch/CPU 后端；切换后端需重新建库。分块及相关测试也依赖共享 BGE tokenizer，清理后必须先运行下载器。

完整语料副本已随代码保留，通常无需再次 `prepare`。默认 `prepare` 从课程目录复制且不覆盖已有资料，后续操作不修改课程源码。

## 真实模型引用回答

通过环境变量、忽略的 `.env` 或明确指定的凭据文件配置模型，不提交密钥：

```sh
.venv/bin/python -m hybrid_rag query --model bge --answer --env-file /absolute/path/to/credentials.env
.venv/bin/python -m hybrid_rag evaluate --model bge --output artifacts/logs/bge-evaluation.json
```

`--answer` 默认使用 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`（默认 deepseek-flash）和 `DEEPSEEK_BASE_URL`（默认官方接口）。仅运行检索不算真实生成模型验收。可选智谱 Embedding 使用 `ZHIPUAI_API_KEY` 与 embedding-3，调用 `build --model zhipu` 时仍使用同一批结构化节点。

## 资料与索引契约

- `corpus/original/`：老师全部 12 份原始资料，含历史、发布说明与草稿 PDF，原样保留。
- `corpus/current/`：唯一入库目录；当前规则、源码与表格，以及注明来源的仍有效结论。
- `corpus/manifest.json`：原始清单、排除理由与派生资料出处。
- `eval/questions.json`：课程八题及 gold，当前答案已随资料更新为 20 分钟。
- `storage/`：重建时生成的向量与 BM25 节点快照，不纳入交付。

Markdown 分块携带完整标题路径、适用范围与正文；Python 按顶层变量、函数和类分块，包含文件名、符号和必要导入；CSV 行携带字段名。长块按共享 BGE tokenizer 的 384 token 预算继续切分。最终资料共 35 节点，最长 244 个 BGE token。

节点 ID 由来源与正文 hash 确定，两路索引共用同一批节点。各 Embedding 模型单独存储向量，BM25 保存 JSON 节点，加载后重建 BM25Okapi。两路各最多召回 20 条，按节点 ID 做 RRF，常数 60，向量/BM25 权重 0.7/0.3，最终取前三条。原始分数分别展示，不直接相加。

只修改当前资料中的登录时长与续期说明，不改原始副本及其他会话时长。每次全量重建双路快照，加载校验后替换单个 CURRENT 指针；失败不发布半成品，资料改变但索引未重建时拒绝查询。旧版本不参与当前检索。变更日志在入库目录之外，重新运行更新时生成。

当前交付语料已经是 20 分钟。若需要完整复现 30 → 20 分钟验收，在下载模型、配置真实 DeepSeek 后执行：

```sh
.venv/bin/python acceptance.py --env-file /absolute/path/to/credentials.env
```

脚本从 30 分钟建库并查询，再改为 20 分钟、重建并在全新 CLI 进程查询；检查引用、旧节点退出、原始资料完整性与八题指标。失败时恢复 20 分钟并重建，不会把失败计为成功。该命令调用真实模型并重新生成验收产物，旧截图不会自动更新。

## 本次最终结果

- 真实 BGE / torch Embedding，生成模型 DeepSeek / deepseek-flash。
- 更新前回答 30 分钟，更新后回答 20 分钟；引用编号、来源与正文对应持久化节点。
- 修改后、重建前正确拒绝过期索引；双路重建后 7 个旧节点退出，仍共享 35 个当前节点。
- 原始 12 文件与课程逐字节一致；登录 20 分钟，支付 5 分钟、匿名 120 分钟、记住我 30 天保持正确。
- 12 项检查通过；包含使用 MockEmbedding 的离线逻辑测试，不以其替代上述真实模型验收。

| 检索方式 | Recall@3 | Hit@3 | 完全命中率@3 |
| --- | --- | --- | --- |
| BGE | 87.5% | 87.5% | 87.5% |
| BM25 | 62.5% | 75% | 50% |
| BGE + BM25 70:30 | 87.5% | 87.5% | 87.5% |

指标按课程八题 gold 文件/标题匹配，不等于答案正确率或通用最优结论。可选重排与模型比较源码及测试保留；历史候选、权重和重排实验记录已归档，默认链路不启用 rerank。

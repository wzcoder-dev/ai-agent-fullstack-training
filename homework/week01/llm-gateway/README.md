# Agent LLM Gateway（Week 01 收官作业）

统一 LLM 访问入口：协议归一（Chat Completions / Responses）、结构化输出（提取 + Schema 校验 + 修复循环）、流式 SSE（checkpoint 重放）、Prompt 模板版本治理、鉴权限流、重试 fallback 熔断、SQLite 用量审计；并提供 **OpenAI Compatible 兼容入口**（吸收自课程 1-7），OpenAI SDK 零改造接入。

设计与决策记录见 [DEVELOPMENT.md](./DEVELOPMENT.md)。

## 安装与启动

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
export DEEPSEEK_API_KEY='你的主模型密钥'          # 供应商密钥只在本进程使用
export DEEPSEEK_BACKUP_API_KEY='你的备用模型密钥'  # 可选
uvicorn app.main:create_app --factory --reload --port 8000
```

模型与上游映射、网关客户端 Key、限流与熔断参数都在 `gateway.example.yaml`（复制为 `gateway.yaml` 自定义，或用 `GATEWAY_CONFIG` 指定路径）。默认客户端 Key：`gw-chat-dev-key-0001`（chat）、`gw-admin-dev-key-0001`（admin）。

## 用 OpenAI SDK 直连（兼容入口）

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="gw-chat-dev-key-0001")
response = client.chat.completions.create(
    model="general-primary",
    messages=[{"role": "user", "content": "解释什么是 LLM Gateway"}],
)
print(response.choices[0].message.content)

# 流式与模型列表同样兼容：
# client.chat.completions.create(..., stream=True, stream_options={"include_usage": True})
# client.models.list()
```

兼容入口复用网关全部治理链路（重试、fallback、结构化修复、限流、审计）；约束见 DEVELOPMENT.md §5.3（工具 role、非 json_schema 的 response_format、流式+schema 组合会被显式拒绝）。

## 非流式调用（自有协议）

```bash
curl http://127.0.0.1:8000/v1/chat \
  -H 'authorization: Bearer gw-chat-dev-key-0001' \
  -H 'content-type: application/json' \
  -d '{"model":"general-primary","messages":[{"role":"user","content":"解释什么是 LLM Gateway"}]}'
```

## Structured Output（出口提取 + Schema 校验 + 修复循环）

```bash
curl http://127.0.0.1:8000/v1/chat \
  -H 'authorization: Bearer gw-chat-dev-key-0001' \
  -H 'content-type: application/json' \
  -d '{"model":"general-primary","messages":[{"role":"user","content":"返回一个答案"}],"response_schema":{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}}'
```

## Streaming（SSE，与 response_schema 互斥）

```bash
curl -N http://127.0.0.1:8000/v1/chat/stream \
  -H 'authorization: Bearer gw-chat-dev-key-0001' \
  -H 'content-type: application/json' \
  -d '{"model":"general-primary","messages":[{"role":"user","content":"用一句话解释流式输出"}]}'
```

事件：`response.started` → `content.delta`（带 seq）→ `response.completed` / `response.failed`。断线后从 checkpoint 重放：

```bash
curl -N http://127.0.0.1:8000/v1/streams/{request_id}/events \
  -H 'authorization: Bearer gw-chat-dev-key-0001' -H 'Last-Event-ID: 2'
```

## Prompt 模板（版本化治理，调用方不能上传正文）

```bash
curl http://127.0.0.1:8000/v1/chat \
  -H 'authorization: Bearer gw-chat-dev-key-0001' \
  -H 'content-type: application/json' \
  -d '{"model":"general-primary","messages":[{"role":"user","content":"需要检索吗"}],"prompt":{"name":"knowledge_decision","version":"v1","variables":{"product_name":"差旅助手"}}}'
```

模板列表 `GET /v1/prompts`；渲染预览（admin）：`POST /v1/prompts/knowledge_decision/v1/render`。

## Responses 风格入口

```bash
curl http://127.0.0.1:8000/v1/responses \
  -H 'authorization: Bearer gw-chat-dev-key-0001' \
  -H 'content-type: application/json' \
  -d '{"model":"openai-responses","input":"返回一个答案","instructions":"你是简洁的中文助手"}'
```

## 管理接口（admin Key）

```bash
curl http://127.0.0.1:8000/v1/traces -H 'authorization: Bearer gw-admin-dev-key-0001'   # 调用审计（不含正文）
curl 'http://127.0.0.1:8000/v1/usage?group_by=model' -H 'authorization: Bearer gw-admin-dev-key-0001'
curl http://127.0.0.1:8000/admin/models -H 'authorization: Bearer gw-admin-dev-key-0001'  # 能力/定价详情
curl http://127.0.0.1:8000/healthz                                                        # 无需鉴权，含熔断状态
curl http://127.0.0.1:8000/readyz                                                         # 就绪检查
```

## 测试

```bash
pytest                # 全离线（MockTransport 驱动）
DEEPSEEK_API_KEY=... pytest tests/test_integration_real.py   # 可选真实上游
```

## Docker

```bash
export DEEPSEEK_API_KEY=...
cp gateway.example.yaml gateway.yaml
docker compose up --build
```

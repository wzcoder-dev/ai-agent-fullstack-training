# 2-4 作业：工具治理契约层——数据模型与接口

> 只生成 Python 数据模型与接口，不实现业务 handler 与引擎。
> 代码：[tool_contracts.py](./tool_contracts.py)（Python 3.12，`dataclass(frozen=True, slots=True)`，Pydantic 2）
> 骨架：[tool_runtime_skeleton.py](./tool_runtime_skeleton.py)（`invoke()` 七检查点空骨架，CP1–CP5 待填充，CP6 已实现）
> 运行：`.venv/bin/python tool_contracts.py`（边界自检）；`.venv/bin/python tool_runtime_skeleton.py`（控制流演示）

## 要求对照

| # | 要求 | 落点 |
| --- | --- | --- |
| 1 | Python 3.12 + frozen/slots dataclass + Pydantic 2 | 全文件 |
| 2 | `ToolDefinition` 含 name、description、Pydantic 参数模型、ToolPolicy、handler、precheck、canonical_target | `ToolDefinition`（七个字段逐一对应） |
| 3 | `ExecutionContext` 含 trace_id、user_id、tenant_id、permissions、allowed_tools、mode、approval_id | `ExecutionContext` |
| 4 | `to_model_tool()` 只返回名称、描述、JSON Schema，禁止暴露 handler/权限/审批 | `to_model_tool()` + `assert_model_tool_safe()` 白名单与敏感词自检 |
| 5 | 类型注解齐全；不实现 PermissionEngine 与 ToolRuntime | 仅 `ToolHandler` / `PrecheckFn` / `CanonicalTargetFn` 类型别名与占位 handler |
| 6 | 解释模型侧对象与运行侧对象的边界 | 见下 |

## 模型侧与运行侧的边界（要求 6）

**模型侧对象（模型可见，不可信）：**

- `to_model_tool()` 的投影：`name` + `description` + `parameters`（JSON Schema）——模型选择工具、生成参数的唯一依据；
- `ToolCall`（模型输出）：id / name / arguments_json——候选动作，必须经校验；
- `ToolResultMessage`（回给模型）：tool_call_id / name / content / is_error——observation，不是最终答案。

**运行侧对象（服务端独占，可信）：**

- `ToolPolicy`：权限要求、风险等级、超时、重试、幂等、结果限长——"执行要求"的声明；
- `precheck` / `canonical_target`：执行前业务裁决钩子、规范化对象标识（审批绑定、幂等去重、审计关联的钥匙）；
- `ExecutionContext`：trace / user / tenant / permissions / allowed_tools / mode / approval_id——"能否执行"的全部判断依据；
- `handler`：业务实现本身，物理上不出服务端。

**边界三条规则：**

1. **投影单向、出口唯一**：Definition → model tool 只经 `to_model_tool()`；模型输出（call）永远不回流进 Definition 或 context。
2. **判断依据只在运行侧**：能不能执行 = `ToolPolicy`（要求）对照 `ExecutionContext`（资格记录）；call 只回答"想干什么"。
3. **泄露可测试**：投影是纯函数，白名单键 + 敏感词断言可以进 CI（`assert_model_tool_safe`）。

> PermissionEngine（拿 policy 对照 context 裁决）与 ToolRuntime（查找→校验→策略→执行→包装）
> 是这两侧之间的执法者，按作业要求本层不实现。

# 2-1 作业：Function Calling 与 Tool Use

课程主线：**模型提出 Tool Call（候选动作）→ Runtime 校验、授权并执行 → 生成带原始 `tool_call_id` 的 Tool Result → Agent Loop 写回结果 → 模型继续决策或完成回答。**

## 4.1 口头问答：两分钟讲清协议闭环

不看代码，回答下面五个问题：

1. Function Calling 与普通函数调用有什么区别？
2. 模型侧 Tool 为什么没有 `execute()`？
3. Input、Output 和 Error Schema 分别约束什么？
4. 为什么权限和审批不能作为模型参数？
5. 两个同名工具调用乱序完成时，`tool_call_id` 怎样防止结果错配？

> 答题记录、评分与参考答案见 [4.1-qa.md](./4.1-qa.md)。

## 4.2 代码：完成一次最小工具调用

模仿瑞幸 AI 开放平台协议接口（https://open.lkcoffee.com/docs ，以 MCP Tool / CLI / Skill 形态暴露点单全流程能力），为 `search_orders` 完成四步：

1. 定义名称、描述和 Input Schema；
2. 生成模型可见的 Tool JSON，确认其中没有 handler、权限和内部依赖；
3. 构造两条不同 `tool_call_id` 的 Tool Call；
4. 分别生成 Tool Result，并断言结果 ID 与原调用一致。

练习只使用模拟订单数据，不实现 Registry、Snapshot、完整权限系统或重试框架。

> 状态：已完成（引导式四关，关卡得分 6 / 6 / 6.5 / 7）。
> 我的版本：[my_minimal_call.py](./my_minimal_call.py)（设计点由我口述、代码落地、冒烟测试全绿）；
> 参考答案：[minimal_call.py](./minimal_call.py)。错点复盘见对话记录，要点：单一事实来源、
> description 不泄露内部字段、信封/内容分层、Tool Result 必带原始 tool_call_id。

## 4.3 审查验收：发现协议错误

审查一段 AI 生成的 Function Calling 代码（[buggy_agent_tool.py](./buggy_agent_tool.py)），至少识别四类问题：

- 把 `user_id`、`role` 或 `approved` 放入 Input Schema，让模型自己填写；
- 把完整 ToolDefinition 或 handler 信息发送给模型；
- 直接相信参数 JSON，没有经过 Pydantic 校验；
- Tool Result 只返回内容，没有原始 `tool_call_id`。

发现以后，说明错误影响的是模型选择、可信边界还是结果关联，并给出相应测试。

> 状态：已完成。审查对象见 [buggy_agent_tool.py](./buggy_agent_tool.py)，
> 结论与测试证据见 [4.3-review.md](./4.3-review.md)，验收测试见 [test_buggy_agent_tool.py](./test_buggy_agent_tool.py)。
> 结果：四类必修问题全部识别（识别 8/10），三个加分点由复盘补齐；6 条契约测试对带病代码全红，即验收通过。

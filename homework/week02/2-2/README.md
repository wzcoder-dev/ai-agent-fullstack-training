# 2-2 作业：Tool Runtime——注册、快照、治理与执行

课程主线：**注册（Registry）→ 发现（Snapshot）→ Schema → Tool Call → prepare → execute → finalize → Tool Result。**

对应课程练习（2-2.pdf 第 23 页"九、课堂练习"）：

## 9.1 口头验收

不看代码，两分钟画出执行链路，并讲清四组区别：

1. Tool Schema 与 ToolDefinition
2. Registry 与 Runtime
3. 工具发现与模型选工具
4. handler、Harness 与 Agent Loop

> 答题记录与评分见 [9.1-qa.md](./9.1-qa.md)。状态：已完成，总分 6.8/10（链路 8 + 四组区别 4/7/8/6）。

## 9.2 代码练习：补出 invoke() 的五个检查点

从 Snapshot 查找 → 参数校验 → 策略拦截 → 受控执行 → 结果包装。七条验收标准：

1. 非法参数不会进入 handler；
2. 权限不足不会产生业务副作用；
3. 模型伪造 `approved=true` 不能绕过批准；
4. 执行阶段不能读取 Registry 的最新版本（快照冻结）；
5. 所有成功和失败结果都保留原 `tool_call_id`；
6. 写操作超时且无幂等保证时，不会被自动重试；
7. 未捕获的内部异常归一化为稳定错误，不泄漏 Python 堆栈。

> 方式（按课程 6.4 节）：先自己写 invoke 伪代码（对象和顺序不能错）→ AI 补实现 →
> 不看解释，只看测试证据（故障注入）。
> 状态：已完成。伪代码 6/10（主要失分：context 全程未使用、审批缺失、重试边界未按幂等性）。
> 实现：[tool_runtime.py](./tool_runtime.py)；演示：[tool_runtime_demo.py](./tool_runtime_demo.py)
> （六条路径输出与课程规格一致，审计记录数 6）；验收：[test_tool_runtime.py](./test_tool_runtime.py)
> 七条标准 8 用例全绿。

# 2-3 作业：MCP 与外部能力接入

课程主线：**MCP 只负责发布（Server）与发现/调用协议（Client）；校验、归一化和决策闭环全部由 Host 的 Runtime 与 Loop 完成。**

## 作业内容（课程"十、课堂练习"）

**10.1 代码**：将订单案例扩展一个 `get_logistics` 工具，五条要求：

1. 仍使用 `orders.get_logistics` 限定名；
2. 找不到物流时返回稳定业务错误，不抛出未处理异常；
3. Host 的 Runtime 不增加任何针对物流工具的特殊分支；
4. 结果超过上限时返回 `RESULT_TOO_LARGE`；
5. 模型先查询订单，只有 `status=shipped` 时才查询物流。

**10.2 口头验收**：

1. 用一句话区分 Function Calling、MCP、Tool Runtime 与 Agent Loop；
2. 能否用测试区分参数拒绝、Tool Error、网络失败和 Loop 恢复；
3. 权限、审计与多租户为什么放在接入之后单独治理。

## 按课堂读代码的四块逻辑推进（不逐行朗读，只抓四条路径）

| 块 | 内容 | 对应文件/职责 | 状态 |
| --- | --- | --- | --- |
| 1 | Server 契约 | [server.py](./server.py)：注册 `orders.get_order` + `orders.get_logistics`，声明 Schema、描述、错误语义 | 设计中 |
| 2 | discover_tools() 的转换 | [host.py](./host.py)：远端工具 → 带限定名的统一 ToolDefinition，handler 闭包调用 `call_tool` | 待做 |
| 3 | ToolRuntime 的执行前边界 | 复用 2-2 的五检查点，证明对物流工具零特殊分支 | 待做 |
| 4 | 结果怎样回到 Loop | 结果归一化、限长、tool_call_id 关联、写回 messages | 待做 |

> 方式：设计点由我口述 → AI 落代码 → 故障注入验收。
> 环境：独立 venv（`.venv/`，含 mcp + jsonschema），运行命令见各文件 docstring。

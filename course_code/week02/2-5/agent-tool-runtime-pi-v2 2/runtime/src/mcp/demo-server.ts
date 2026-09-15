import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

const server = new Server(
  { name: "knowledge-demo", version: "1.0.0" },
  { capabilities: { tools: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "knowledge_search",
      description: "搜索客服知识库中的处理规则",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", minLength: 2, description: "要搜索的问题" },
        },
        required: ["query"],
        additionalProperties: false,
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name !== "knowledge_search") {
    return { isError: true, content: [{ type: "text", text: "unknown tool" }] };
  }
  const query = String(request.params.arguments?.query ?? "");
  const text = query.includes("退款")
    ? "退款属于写操作：先做 RBAC，再绑定订单、金额与 tool_call_id 发起人工确认。"
    : "未找到精确流程，请创建客服工单并保留 Trace。";
  return {
    content: [{ type: "text", text }],
    structuredContent: { query, answer: text, source: "support-handbook-v1" },
  };
});

await server.connect(new StdioServerTransport());

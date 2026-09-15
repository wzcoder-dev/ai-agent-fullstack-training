import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { RiskLevel } from "../domain.js";
import { ToolRuntimeError } from "../domain.js";
import type { ToolRegistry } from "../registry.js";

interface McpServerBaseConfig {
  id: string;
  trust: "trusted" | "untrusted";
  defaultRisk: RiskLevel;
  permissions: string[];
}

export type McpServerConfig = McpServerBaseConfig & (
  | {
      type: "stdio";
      command: string;
      args: string[];
      cwd?: string;
    }
  | {
      type: "streamablehttp";
      url: string;
      headers: Record<string, string>;
    }
);

interface Connection {
  client: Client;
}

export class McpAdapterPlugin {
  private readonly connections: Connection[] = [];

  constructor(private readonly servers: McpServerConfig[]) {}

  async attach(registry: ToolRegistry): Promise<void> {
    for (const server of this.servers) {
      if (server.trust !== "trusted") {
        throw new Error(`MCP server is not trusted: ${server.id}`);
      }
      const client = new Client({ name: "agent-tool-runtime", version: "2.0.0" }, { capabilities: {} });
      const transport = server.type === "stdio"
        ? new StdioClientTransport({ command: server.command, args: server.args, cwd: server.cwd })
        : new StreamableHTTPClientTransport(new URL(server.url), {
            requestInit: { headers: resolveHeaders(server.headers) },
          });
      await client.connect(transport);
      this.connections.push({ client });
      const discovered = await client.listTools();

      for (const remoteTool of discovered.tools) {
        const internalName = `mcp.${server.id}.${remoteTool.name}`;
        registry.register({
          metadata: {
            internalName,
            description: remoteTool.description ?? `MCP tool ${remoteTool.name}`,
            version: "mcp",
            parameters: remoteTool.inputSchema as never,
            risk: server.defaultRisk,
            permissions: server.permissions,
            requiresConfirmation: server.defaultRisk !== "read",
            timeoutMs: 10_000,
            maxRetries: server.defaultRisk === "read" ? 1 : 0,
            idempotent: server.defaultRisk === "read",
            source: "mcp",
          },
          async handler({ args, signal }) {
            try {
              const result = await client.callTool(
                { name: remoteTool.name, arguments: args },
                undefined,
                { signal, timeout: 10_000 },
              );
              if (result.isError) {
                throw new ToolRuntimeError("MCP_TOOL_ERROR", normalizeMcpText(result.content), false);
              }
              if ("structuredContent" in result && result.structuredContent) return result.structuredContent;
              return { content: normalizeMcpText(result.content), server_id: server.id };
            } catch (error) {
              if (error instanceof ToolRuntimeError) throw error;
              throw new ToolRuntimeError(
                "MCP_UNAVAILABLE",
                error instanceof Error ? error.message : "MCP 调用失败",
                true,
              );
            }
          },
        });
      }
    }
  }

  async close(): Promise<void> {
    await Promise.all(this.connections.map(({ client }) => client.close()));
    this.connections.length = 0;
  }
}

function resolveHeaders(headers: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(headers).map(([name, template]) => [
    name,
    template.replace(/\$\{([A-Z][A-Z0-9_]*)\}/g, (_match, envName: string) => {
      const value = process.env[envName];
      if (!value) throw new Error(`MCP_AUTH_MISSING: 请设置环境变量 ${envName}`);
      return value;
    }),
  ]));
}

function normalizeMcpText(content: unknown): string {
  if (!Array.isArray(content)) return JSON.stringify(content);
  return content
    .map((item) => {
      if (item && typeof item === "object" && "text" in item) return String(item.text);
      return JSON.stringify(item);
    })
    .join("\n");
}

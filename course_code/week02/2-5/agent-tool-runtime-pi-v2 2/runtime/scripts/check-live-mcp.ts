import { buildPlatform } from "../src/platform.js";

if (!process.env.LUCKIN_MCP_TOKEN) {
  process.stderr.write("LIVE_MCP_CREDENTIAL_MISSING: LUCKIN_MCP_TOKEN\n");
  process.exit(2);
}

const platform = await buildPlatform({ connectMcp: true });
try {
  const tools = platform.registry.list().filter((tool) => tool.metadata.source === "mcp");
  if (tools.length === 0) throw new Error("my-coffee 未返回任何工具");
  process.stdout.write(`${JSON.stringify(tools.map((tool) => ({
    internal_name: tool.metadata.internalName,
    model_name: tool.modelName,
    risk: tool.metadata.risk,
    requires_confirmation: tool.metadata.requiresConfirmation,
  })), null, 2)}\n`);
} finally {
  await platform.close();
}

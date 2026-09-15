import type { ExecutionContext } from "../domain.js";
import type { PlatformConfig } from "../config.js";

export async function renderSystemPrompt(config: PlatformConfig, context: ExecutionContext): Promise<{
  prompt: string;
  version: string;
  contentHash: string;
}> {
  const apiKey = process.env[config.gateway.apiKeyEnv];
  if (!apiKey) throw new Error(`Missing ${config.gateway.apiKeyEnv}`);
  const base = config.gateway.baseUrl.replace(/\/$/, "").replace(/\/v1$/, "");
  const response = await fetch(`${base}/v1/prompts/${encodeURIComponent(config.gateway.promptName)}/render`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({
      version: config.gateway.promptVersion,
      variables: {
        tenant_id: context.tenantId,
        user_id: context.userId,
        agent_name: context.agentName,
      },
    }),
  });
  if (!response.ok) throw new Error(`Prompt Registry returned ${response.status}: ${await response.text()}`);
  const payload = (await response.json()) as any;
  return { prompt: payload.prompt, version: payload.version, contentHash: payload.content_hash };
}

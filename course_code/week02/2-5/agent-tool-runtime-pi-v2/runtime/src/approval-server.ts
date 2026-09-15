#!/usr/bin/env node
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { buildPlatform } from "./platform.js";

const configPath = process.env.PLATFORM_CONFIG ?? "../config/platform.yaml";
const platform = await buildPlatform({ configPath, connectMcp: process.env.APPROVAL_CONNECT_MCP !== "false" });
const { host, port, apiKeyEnv } = platform.config.approvalApi;
const apiKey = process.env[apiKeyEnv];
if (!apiKey) throw new Error(`Missing ${apiKeyEnv}`);

const server = createServer(async (request, response) => {
  try {
    if (request.url === "/health" && request.method === "GET") return json(response, 200, { status: "ok" });
    if (request.headers.authorization !== `Bearer ${apiKey}`) return json(response, 401, { code: "UNAUTHORIZED" });
    const url = new URL(request.url ?? "/", `http://${request.headers.host ?? "localhost"}`);

    if (request.method === "GET" && url.pathname === "/approvals") {
      return json(response, 200, platform.store.listApprovals(url.searchParams.get("status") ?? undefined));
    }
    if (request.method === "GET" && url.pathname === "/audit") {
      return json(response, 200, platform.store.listAudit(url.searchParams.get("trace_id") ?? undefined));
    }
    if (request.method === "GET" && url.pathname.startsWith("/traces/")) {
      return json(response, 200, platform.store.getTrace(decodeURIComponent(url.pathname.slice(8))));
    }

    const decisionMatch = url.pathname.match(/^\/approvals\/([^/]+)\/decision$/);
    if (request.method === "POST" && decisionMatch) {
      const body = await readJson(request);
      if (body.decision !== "approve" && body.decision !== "reject") {
        return json(response, 400, { code: "INVALID_DECISION" });
      }
      return json(
        response,
        200,
        platform.store.decideApproval(decodeURIComponent(decisionMatch[1]), body.decision, String(body.actor ?? "api")),
      );
    }
    const executeMatch = url.pathname.match(/^\/approvals\/([^/]+)\/execute$/);
    if (request.method === "POST" && executeMatch) {
      const result = await platform.runtime.executeApproval(decodeURIComponent(executeMatch[1]));
      return json(response, result.ok ? 200 : 409, result);
    }
    return json(response, 404, { code: "NOT_FOUND" });
  } catch (error) {
    return json(response, 400, { code: "REQUEST_FAILED", message: error instanceof Error ? error.message : String(error) });
  }
});

server.listen(port, host, () => process.stdout.write(`approval-api http://${host}:${port}\n`));

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.once(signal, async () => {
    server.close();
    await platform.close();
    process.exit(0);
  });
}

async function readJson(request: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  if (chunks.length === 0) return {};
  const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("JSON body must be an object");
  return parsed;
}

function json(response: ServerResponse, status: number, value: unknown): void {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(value));
}

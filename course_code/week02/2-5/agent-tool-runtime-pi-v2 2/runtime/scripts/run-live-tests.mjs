import { spawn } from "node:child_process";
import { resolve } from "node:path";

for (const name of ["DEEPSEEK_API_KEY", "GATEWAY_API_KEY"]) {
  if (!process.env[name]) {
    process.stderr.write(`LIVE_TEST_CREDENTIAL_MISSING: ${name}\n`);
    process.exit(2);
  }
}

const gatewayDirectory = resolve("../gateway");
const python = process.env.PYTHON_BIN ?? resolve("../.venv/bin/python");
const gateway = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"], {
  cwd: gatewayDirectory,
  env: {
    ...process.env,
    GATEWAY_CONFIG: resolve("../config/gateway.yaml"),
    LITELLM_LOCAL_MODEL_COST_MAP: "True",
  },
  stdio: ["ignore", "pipe", "pipe"],
});
gateway.stdout.pipe(process.stderr);
gateway.stderr.pipe(process.stderr);

try {
  await waitForHealth("http://127.0.0.1:8000/health", 20_000);
  const test = spawn(process.execPath, ["./node_modules/vitest/vitest.mjs", "run", "tests/live/deepseek.test.ts"], {
    stdio: "inherit",
    env: process.env,
  });
  const exitCode = await new Promise((resolveExit) => test.once("exit", resolveExit));
  process.exitCode = Number(exitCode ?? 1);
} finally {
  gateway.kill("SIGTERM");
}

async function waitForHealth(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Server is still starting.
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, 250));
  }
  throw new Error("Gateway did not become healthy");
}

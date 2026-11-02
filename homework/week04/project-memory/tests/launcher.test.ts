import assert from "node:assert/strict";
import { test } from "node:test";
import { cpSync, mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";

test("手动启动器从任意目录经快捷符号链接启动，仍进入实际样本并传入扩展", () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "memory-launcher-")));
  try {
    const run = join(root, "runs/manual-test");
    const project = join(run, "login-project");
    const bin = join(root, "node_modules/.bin");
    mkdirSync(project, { recursive: true });
    mkdirSync(bin, { recursive: true });
    const launcher = join(run, "start-pi.command");
    cpSync(new URL("../scripts/manual-launcher.zsh", import.meta.url), launcher);
    // 在真实 CLI 边界捕获工作目录与参数，无需模型、凭证或用户会话。
    writeFileSync(join(bin, "pi"), '#!/usr/bin/env node\nconsole.log(JSON.stringify({cwd:process.cwd(),args:process.argv.slice(2),path:process.env.PATH}));\n', { mode: 0o755 });
    const shortcut = join(root, "runs/start-manual.command");
    symlinkSync(launcher, shortcut);
    for (const entry of [launcher, shortcut]) {
      const result = spawnSync("zsh", [entry, "--name", "regression"], { cwd: tmpdir(), encoding: "utf8" });
      assert.equal(result.status, 0, result.stderr);
      const state = JSON.parse(result.stdout) as {cwd: string; args: string[]; path: string};
      assert.equal(state.cwd, project);
      assert.ok(state.path.startsWith(join(root, ".venv/bin")));
      assert.ok(state.args.includes("--no-extensions"));
      assert.equal(state.args[state.args.indexOf("-e") + 1], join(root, "src/extension.ts"));
      assert.equal(state.args[state.args.indexOf("--session-dir") + 1], join(root, "artifacts/manual-test/sessions"));
      assert.deepEqual(state.args.slice(-2), ["--name", "regression"]);
    }
  } finally { rmSync(root, { recursive: true, force: true }); }
});

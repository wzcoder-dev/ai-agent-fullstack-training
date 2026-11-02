/** 真实 pi 新进程验收；不读取或打印凭证，只使用 pi 已配置的 provider。 */
import assert from "node:assert/strict";
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { ProjectMemory } from "../src/store.ts";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const stamp = new Date().toISOString().replace(/[:.]/g, "-");
const project = join(root, "runs", stamp, "login-project");
const out = join(root, "artifacts", `acceptance-${stamp}`);
mkdirSync(out, { recursive: true });
cpSync(join(root, "fixtures/login-project"), project, { recursive: true });
const sessionDir = join(out, "sessions");
const provider = process.env.MEMORY_VERIFY_PROVIDER ?? "deepseek";
const model = process.env.MEMORY_VERIFY_MODEL ?? "deepseek-flash";
const tools = ["read", "bash", "edit", "write", "project_memory_create", "project_memory_search", "project_memory_read", "project_memory_update", "project_memory_delete"];
const env = { ...process.env, PATH: `${join(root, ".venv/bin")}:${process.env.PATH ?? ""}`, PI_OFFLINE: "1", PI_TELEMETRY: "0" };

async function command(bin: string, args: string[], cwd: string, label: string, timeout = 180_000) {
  return await new Promise<{ stdout: string; stderr: string; code: number }>((resolvePromise, reject) => {
    const child = spawn(bin, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = ""; let stderr = "";
    const timer = setTimeout(() => { child.kill("SIGTERM"); }, timeout);
    child.stdout.on("data", (data: Buffer) => { stdout += data.toString(); });
    child.stderr.on("data", (data: Buffer) => { stderr += data.toString(); });
    child.on("error", error => { clearTimeout(timer); reject(error); });
    child.on("close", code => {
      clearTimeout(timer);
      writeFileSync(join(out, `${label}.stdout.log`), stdout);
      writeFileSync(join(out, `${label}.stderr.log`), stderr);
      if (code !== 0) reject(new Error(`${label} 退出码 ${code}；见 ${out}`));
      else resolvePromise({ stdout, stderr, code });
    });
  });
}

function parseEvents(stdout: string): Record<string, unknown>[] {
  return stdout.split("\n").filter(line => line.startsWith("{")).map(line => JSON.parse(line) as Record<string, unknown>);
}
function calls(events: Record<string, unknown>[]): string[] {
  return events.filter(event => event.type === "tool_execution_start").map(event => String(event.toolName));
}
async function phase(label: string, prompt: string, requiredTools: string[]) {
  console.log(`开始 ${label}：启动独立 pi 进程`);
  const args = ["--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files", "--offline",
    "-e", join(root, "src/extension.ts"), "--provider", provider, "--model", model, "--thinking", "off", "--mode", "json", "--session-dir", sessionDir,
    "--tools", tools.join(","), "--print", prompt];
  const run = await command("pi", args, project, label, 300_000);
  const events = parseEvents(run.stdout);
  const used = calls(events);
  for (const name of requiredTools) assert.ok(used.includes(name), `${label} 未实际调用 ${name}`);
  const errors = events.filter(event => event.type === "tool_execution_end" && event.isError === true);
  assert.equal(errors.filter(event => String(event.toolName).startsWith("project_memory_")).length, 0, `${label} 出现记忆工具失败，检查日志`);
  const assistantErrors = events.filter(event => event.type === "message_end" && (event.message as {stopReason?: string})?.stopReason === "error");
  assert.equal(assistantErrors.length, 0, `${label} 模型返回错误`);
  console.log(`${label} 完成；实际工具：${used.join(", ")}`);
  return { label, calls: used, processExitCode: run.code, otherToolErrors: errors };
}

const stages: unknown[] = [];
const store = new ProjectMemory(project);
stages.push(await phase("01-create", "读取当前项目的会话测试文件和测试说明，将测试路径和运行命令保存到项目长期记忆，附上来源。返回实际保存路径。请先运行说明中的测试命令，将实际结果一起记录。使用 project_memory_create 工具，稳定标识为 testing/session-entry；不要手写记忆目录文件。不要修改业务文件。", ["project_memory_create", "read", "bash"]));
assert.ok(store.read("testing/session-entry").body.includes("tests/test_session.py"));
cpSync(store.bundle, join(out, "01-memory"), { recursive: true });

// 提示词不包含旧测试路径或记忆标识；不使用 --continue、--resume 或历史消息。
stages.push(await phase("02-new-session", "从当前项目长期记忆中查找会话测试入口，再核对当前文件是否存在。先调用记忆搜索和读取工具，随后用文件读取或命令核对存在性；给出记忆标识、当前入口、运行命令、来源及核对结果。不要修改任何文件。", ["project_memory_search", "project_memory_read"]));

stages.push(await phase("03-update", "将会话测试迁移到 tests/auth/test_session.py，同步修改测试说明，运行新测试命令。根据实际结果更新原来的长期记忆。先搜索并读取原记忆，保持原记忆标识，用 project_memory_update 完整更新正文和来源，不能新增一条替代记录，不能手写记忆文件。当前记忆和测试说明仅保留新的入口、命令及实际结果，不写迁移历史或旧路径文字；历史由记忆日志和会话记录保留。完成后再次用记忆工具读取，确认旧测试入口已从当前记录退出。检查旧文件不存在请用 test ! -e，避免用 ls 不存在文件产生预期内的非零退出。", ["project_memory_search", "project_memory_read", "project_memory_update", "bash"]));
assert.equal(existsSync(join(project, "tests/test_session.py")), false);
assert.equal(existsSync(join(project, "tests/auth/test_session.py")), true);
const guide = readFileSync(join(project, "docs/testing.md"), "utf8");
assert.ok(guide.includes("tests/auth/test_session.py"));
assert.ok(!guide.includes("tests/test_session.py"));
const updated = store.read("testing/session-entry");
assert.ok(updated.body.includes("tests/auth/test_session.py"));
assert.ok(!updated.body.includes("tests/test_session.py"), "当前记录仍包含过期测试入口");
assert.deepEqual(store.search("tests/test_session.py"), []);
assert.equal(store.search("tests/auth/test_session.py").length, 1);
const testRun = await command(join(root, ".venv/bin/python"), ["-m", "pytest", "tests/auth/test_session.py", "-q"], project, "04-independent-test");
assert.match(testRun.stdout, /5 passed/);
cpSync(store.bundle, join(out, "03-memory"), { recursive: true });
cpSync(project, join(out, "updated-project"), { recursive: true, filter: path => !["__pycache__", ".pytest_cache"].some(part => path.split("/").includes(part)) });

stages.push(await phase("05-delete", "验证项目记忆的删除能力：创建一条临时记忆，标识为 verification/delete-check，正文含 DELETE_CHECK_ONLY，来源使用当前真实测试说明；然后用 project_memory_delete 删除。最后搜索 DELETE_CHECK_ONLY，应无结果。不要删除或修改会话测试入口那条正式记忆，也不要调用 read 读取已删除记录。返回实际删除路径和搜索结果。", ["project_memory_create", "project_memory_delete", "project_memory_search"]));
assert.equal(existsSync(join(store.bundle, "verification/delete-check.md")), false);
assert.deepEqual(store.search("DELETE_CHECK_ONLY"), []);
assert.ok(store.read("testing/session-entry"));

const sessions = readdirSync(sessionDir).filter(name => name.endsWith(".jsonl"));
const ids = sessions.map(name => JSON.parse(readFileSync(join(sessionDir, name), "utf8").split("\n")[0]).id as string);
assert.equal(sessions.length, 4);
assert.equal(new Set(ids).size, 4);
writeFileSync(join(out, "summary.json"), JSON.stringify({ status: "passed", generatedAt: new Date().toISOString(), piVersion: "0.87.1", provider, model, project, stages, independentTest: testRun.stdout.trim(), sessionIds: ids,
  checks: { freshSessions: true, stableRecordId: "testing/session-entry", oldTestRemoved: true, oldEntryNotSearchable: true, currentTestPassed: true, deletedRecordNotSearchable: true } }, null, 2));
writeFileSync(join(root, "artifacts/latest.json"), JSON.stringify({ path: out }, null, 2));
console.log(`全部真实验收通过：${out}`);

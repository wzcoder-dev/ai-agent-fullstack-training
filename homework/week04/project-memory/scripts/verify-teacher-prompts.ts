/** 原始题面验收：提示词逐字使用老师原文，断言不向模型提供额外指令。 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { ProjectMemory, type MemoryRecord } from "../src/store.ts";

const prompts = [
  "读取当前项目的会话测试文件和测试说明，将测试路径和运行命令保存到项目长期记忆，附上来源。返回实际保存路径。",
  "从当前项目长期记忆中查找会话测试入口，再核对当前文件是否存在。",
  "将会话测试迁移到 tests/auth/test_session.py，同步修改测试说明，运行新测试命令。根据实际结果更新原来的长期记忆。",
];
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const stamp = new Date().toISOString().replace(/[:.]/g, "-");
const project = join(root, "runs", `teacher-${stamp}`, "login-project");
const out = join(root, "artifacts", `teacher-${stamp}`);
const sessionDir = join(out, "sessions");
mkdirSync(sessionDir, { recursive: true });
cpSync(join(root, "fixtures/login-project"), project, { recursive: true,
  filter: path => ![".pi", "__pycache__", ".pytest_cache", ".venv", "wheels"].some(part => path.split("/").includes(part)) });
const env = { ...process.env, PATH: `${join(root, ".venv/bin")}:${process.env.PATH ?? ""}`, PI_OFFLINE: "1", PI_TELEMETRY: "0" };
const provider = process.env.MEMORY_VERIFY_PROVIDER ?? "deepseek";
const model = process.env.MEMORY_VERIFY_MODEL ?? "deepseek-flash";
const store = new ProjectMemory(project);
writeFileSync(join(out, "prompts.json"), JSON.stringify(prompts, null, 2));
writeFileSync(join(out, "source-before.json"), JSON.stringify(Object.fromEntries(["src/extension.ts", "src/store.ts"].map(path => [path, readFileSync(join(root, path), "utf8")])), null, 2));

type Event = Record<string, unknown>;
async function command(bin: string, args: string[], label: string) {
  return await new Promise<{code: number | null; stdout: string; stderr: string; timedOut: boolean}>(resolvePromise => {
    const child = spawn(bin, args, { cwd: project, env, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = ""; let stderr = ""; let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill("SIGTERM"); }, 180_000);
    child.stdout.on("data", data => { stdout += data.toString(); });
    child.stderr.on("data", data => { stderr += data.toString(); });
    child.on("error", error => { stderr += String(error); });
    child.on("close", code => {
      clearTimeout(timer);
      writeFileSync(join(out, `${label}.stdout.log`), stdout);
      writeFileSync(join(out, `${label}.stderr.log`), stderr);
      resolvePromise({ code, stdout, stderr, timedOut });
    });
  });
}
function records(): MemoryRecord[] {
  const paths: string[] = [];
  const walk = (dir: string) => {
    if (!existsSync(dir)) return;
    for (const file of readdirSync(dir, { withFileTypes: true })) {
      if (file.name.startsWith(".")) continue;
      const path = join(dir, file.name);
      if (file.isDirectory()) walk(path);
      else if (file.isFile() && file.name.endsWith(".md") && !["index.md", "log.md"].includes(file.name)) paths.push(path);
    }
  };
  walk(store.bundle);
  return paths.map(path => store.read(relative(store.bundle, path).replace(/\.md$/, "")));
}
function toolCalls(events: Event[]) {
  return events.filter(event => event.type === "tool_execution_start").map(event => ({ name: String(event.toolName), args: event.args as Record<string, unknown> }));
}
function finalAnswer(events: Event[]) {
  return events.filter(event => event.type === "message_end" && (event.message as {role?: string})?.role === "assistant")
    .map(event => ((event.message as {content?: {type: string; text?: string}[]}).content ?? []).filter(block => block.type === "text").map(block => block.text ?? "").join("\n")).filter(Boolean).at(-1) ?? "";
}
const stages: Record<string, unknown>[] = [];
let initial: MemoryRecord[] = [];
for (let i = 0; i < prompts.length; i++) {
  const label = `${String(i + 1).padStart(2, "0")}-${["create", "recall", "update"][i]}`;
  console.log(`开始 ${label}：只发送老师原始提示词`);
  const result = await command(join(root, "node_modules/.bin/pi"), ["--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files", "--offline",
    "-e", join(root, "src/extension.ts"), "--provider", provider, "--model", model, "--thinking", "off", "--mode", "json", "--session-dir", sessionDir, "--print", prompts[i]], label);
  const events: Event[] = result.stdout.split("\n").filter(line => line.startsWith("{")).map(line => JSON.parse(line) as Event);
  const calls = toolCalls(events);
  const errors = events.filter(event => event.type === "tool_execution_end" && event.isError === true);
  const answer = finalAnswer(events);
  writeFileSync(join(out, `${label}.answer.md`), answer);
  const current = records();
  if (existsSync(store.bundle)) cpSync(store.bundle, join(out, `${label}-memory`), { recursive: true });
  const checks: Record<string, boolean> = {
    processSucceeded: result.code === 0 && !result.timedOut,
    modelSucceeded: !events.some(event => event.type === "message_end" && ["error", "aborted"].includes(String((event.message as {stopReason?: string})?.stopReason))),
  };
  if (i === 0) {
    initial = current;
    checks.memoryPersisted = current.length > 0;
    checks.testPathAndCommandSaved = current.some(record => record.body.includes("tests/test_session.py") && /python\s+-m\s+pytest\s+tests\/test_session\.py\s+-q/.test(record.body));
    checks.sourceSaved = current.some(record => JSON.stringify(record.metadata.sources).includes("testing.md") && JSON.stringify(record.metadata.sources).includes("test_session.py"));
    checks.actualPathReturned = current.some(record => answer.includes(record.path));
  } else if (i === 1) {
    const readIndex = calls.findIndex(call => call.name === "project_memory_read");
    checks.memoryReadInNewSession = readIndex >= 0;
    checks.readExistingId = calls.some(call => call.name === "project_memory_read" && initial.some(record => record.id === call.args?.id));
    checks.fileCheckedAfterMemoryRead = readIndex >= 0 && calls.slice(readIndex + 1).some(call =>
      ["bash", "read"].includes(call.name) && JSON.stringify(call.args).includes("test_session.py"));
    checks.answerReportsExistingFile = answer.includes("test_session.py") && /存在|存在性|exist/i.test(answer);
  } else {
    checks.oldTestRemoved = !existsSync(join(project, "tests/test_session.py"));
    checks.newTestExists = existsSync(join(project, "tests/auth/test_session.py"));
    checks.guideUpdated = readFileSync(join(project, "docs/testing.md"), "utf8").includes("python -m pytest tests/auth/test_session.py -q");
    checks.sameRecordUpdated = initial.length > 0 && initial.some(before => current.some(after => after.id === before.id && after.body !== before.body && after.body.includes("tests/auth/test_session.py")));
    checks.noReplacementRecordCreated = current.length === initial.length;
    const testIndex = calls.findIndex(call => call.name === "bash" && /python\s+-m\s+pytest\s+tests\/auth\/test_session\.py\s+-q/.test(String(call.args?.command)));
    const updateIndex = calls.findIndex(call => call.name === "project_memory_update");
    checks.testRunBeforeUpdate = testIndex >= 0 && updateIndex > testIndex;
    checks.currentCommandSaved = current.some(record => record.body.includes("python -m pytest tests/auth/test_session.py -q"));
    checks.actualResultSaved = current.some(record => /5\s+passed/.test(record.body));
    checks.sourcesUpdated = current.some(record => JSON.stringify(record.metadata.sources).includes("tests/auth/test_session.py"));
    // 历史说明里提到旧路径不是题面失败；单独记录，不以字符串完全消失额外收紧老师要求。
    writeFileSync(join(out, "old-path-search.json"), JSON.stringify(store.search("tests/test_session.py"), null, 2));
  }
  stages.push({ label, prompt: prompts[i], checks, passed: Object.values(checks).every(Boolean), calls, errors, recordIds: current.map(record => record.id) });
  writeFileSync(join(out, "progress.json"), JSON.stringify(stages, null, 2));
  console.log(`${label}：${Object.values(checks).every(Boolean) ? "通过" : "存在问题"}；${calls.map(call => call.name).join(", ")}`);
}
const independent = await command(join(root, ".venv/bin/python"), ["-m", "pytest", "tests/auth/test_session.py", "-q"], "04-independent-test");
const sessions = readdirSync(sessionDir).filter(name => name.endsWith(".jsonl"));
const sessionIds = sessions.map(name => JSON.parse(readFileSync(join(sessionDir, name), "utf8").split("\n")[0]).id);
assert.equal(new Set(sessionIds).size, 3);
const sourceBefore = JSON.parse(readFileSync(join(out, "source-before.json"), "utf8")) as Record<string, string>;
assert.ok(Object.entries(sourceBefore).every(([path, text]) => readFileSync(join(root, path), "utf8") === text), "验证中实现发生了变更");
const passed = stages.every(stage => stage.passed === true) && independent.code === 0 && /5 passed/.test(independent.stdout);
const summary = { status: passed ? "passed" : "issues-found", exactTeacherPrompts: true, implementationChanged: false, project, provider, model, stages, sessionIds, independentTest: independent.stdout.trim() };
writeFileSync(join(out, "summary.json"), JSON.stringify(summary, null, 2));
writeFileSync(join(root, "artifacts/teacher-latest.json"), JSON.stringify({ path: out, status: summary.status }, null, 2));
console.log(`原词验证完成：${summary.status}；${out}`);
process.exitCode = passed ? 0 : 1;

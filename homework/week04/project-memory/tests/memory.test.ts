import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, symlinkSync, renameSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { parse } from "yaml";
import { MemoryError, ProjectMemory, type MemoryInput } from "../src/store.ts";
import extension from "../src/extension.ts";
import type { ExtensionAPI, ToolDefinition } from "@earendil-works/pi-coding-agent";

function fixture() {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "week04-memory-test-")));
  writeFileSync(join(root, "testing.md"), "测试说明");
  const input: MemoryInput = { type: "Playbook", title: "会话测试入口", description: "会话测试运行方法", body: "测试路径：tests/test_session.py\n命令：python -m pytest tests/test_session.py -q\n结果：未执行", sources: [{ path: "testing.md" }] };
  return { root, input, store: new ProjectMemory(root) };
}
function expectCode(code: string) { return (error: unknown) => error instanceof MemoryError && error.code === code; }

test("持久化 OKF 元信息、来源 URL、索引与日志，并能关键词查找", () => {
  const { root, input, store } = fixture();
  try {
    const saved = store.create("testing/session-entry", input);
    assert.equal(saved.path, join(root, ".pi/project-memory/testing/session-entry.md"));
    const record = store.read("testing/session-entry");
    assert.equal(record.body, input.body);
    assert.match(String((record.metadata.sources as {resource: string}[])[0].resource), /^file:\/\//);
    assert.deepEqual(store.search("会话 测试").map(hit => hit.id), [record.id]);
    assert.deepEqual(store.search("不存在"), []);
    assert.match(readFileSync(join(store.bundle, "index.md"), "utf8"), /okf_version: "0.2"/);
    assert.match(readFileSync(join(store.bundle, "log.md"), "utf8"), /\*\*Create\*\*/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("真正的新 Node 进程能从磁盘读取，与原实例无共享状态", () => {
  const { root, input, store } = fixture();
  try {
    store.create("testing/session-entry", input);
    const url = new URL("../src/store.ts", import.meta.url).href;
    const child = spawnSync(process.execPath, ["--input-type=module", "-e", `import {ProjectMemory} from ${JSON.stringify(url)}; console.log(JSON.stringify(new ProjectMemory(process.argv[1]).read('testing/session-entry')));`, root], { encoding: "utf8" });
    assert.equal(child.status, 0, child.stderr);
    assert.equal(JSON.parse(child.stdout).body, input.body);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("项目同名记录隔离，工作目录不自动向上归属父仓库", () => {
  const a = fixture(); const b = fixture();
  try {
    a.store.create("testing/session-entry", a.input);
    b.store.create("testing/session-entry", { ...b.input, body: "另一个项目" });
    assert.equal(a.store.read("testing/session-entry").body, a.input.body);
    assert.equal(b.store.read("testing/session-entry").body, "另一个项目");
    const child = join(a.root, "child"); mkdirSync(child);
    assert.deepEqual(new ProjectMemory(child).search("会话"), []);
  } finally { rmSync(a.root, { recursive: true, force: true }); rmSync(b.root, { recursive: true, force: true }); }
});

test("迁移后同 ID 完整替换、旧事实退出搜索、日志保留动作", () => {
  const { root, input, store } = fixture();
  try {
    store.create("testing/session-entry", input);
    const recordPath = store.read("testing/session-entry").path;
    const text = readFileSync(recordPath, "utf8").replace("type: Playbook", "type: Playbook\ncustom_field: preserved");
    writeFileSync(recordPath, text);
    store.update("testing/session-entry", { ...input, body: "新入口 tests/auth/test_session.py；实际运行：5 passed" });
    assert.equal(store.read("testing/session-entry").metadata.custom_field, "preserved");
    assert.deepEqual(store.search("tests/test_session.py"), []);
    assert.equal(store.search("tests/auth/test_session.py").length, 1);
    assert.match(readFileSync(join(store.bundle, "log.md"), "utf8"), /\*\*Update\*\*/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("重复创建和不存在的更新不覆盖、不隐式新增", () => {
  const { root, input, store } = fixture();
  try {
    assert.throws(() => store.update("missing", input), expectCode("NOT_FOUND"));
    assert.equal(existsSync(join(store.bundle, "missing.md")), false);
    store.create("existing", input);
    assert.throws(() => store.create("existing", { ...input, body: "意外覆盖" }), expectCode("ALREADY_EXISTS"));
    assert.equal(store.read("existing").body, input.body);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("删除后文件、索引与搜索一致，日志不保留正文", () => {
  const { root, input, store } = fixture();
  try {
    const { path } = store.create("temporary", { ...input, body: "DELETE_BODY_SENTINEL" });
    store.delete("temporary");
    assert.equal(existsSync(path), false);
    assert.deepEqual(store.search("DELETE_BODY_SENTINEL"), []);
    assert.throws(() => store.read("temporary"), expectCode("NOT_FOUND"));
    assert.throws(() => store.delete("temporary"), expectCode("NOT_FOUND"));
    assert.doesNotMatch(readFileSync(join(store.bundle, "index.md"), "utf8"), /temporary/);
    const log = readFileSync(join(store.bundle, "log.md"), "utf8");
    assert.match(log, /\*\*Delete\*\*/); assert.doesNotMatch(log, /DELETE_BODY_SENTINEL/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("参数、来源、保留名与目录穿越均被校验", () => {
  const { root, input, store } = fixture();
  try {
    for (const id of ["../escape", "/tmp/absolute", "a/../../b", "index", "a/log", "LOG", "a.md", ""]) {
      assert.throws(() => store.create(id, input), expectCode("INVALID_ID"));
    }
    assert.throws(() => store.create("valid", { ...input, type: " " }), expectCode("INVALID_INPUT"));
    assert.throws(() => store.create("valid", { ...input, sources: [] }), expectCode("INVALID_INPUT"));
    assert.throws(() => store.create("valid", { ...input, sources: [{ path: "missing.md" }] }), expectCode("INVALID_SOURCE"));
    assert.throws(() => store.create("valid", { ...input, sources: [{ path: "../testing.md" }] }), expectCode("INVALID_SOURCE"));
    assert.throws(() => store.search("  "), expectCode("INVALID_INPUT"));
    assert.throws(() => store.search("测试", 0), expectCode("INVALID_INPUT"));
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("记忆目录与来源符号链接均拒绝，阻止跨项目读写", () => {
  const a = fixture(); const b = fixture();
  try {
    symlinkSync(join(b.root, "testing.md"), join(a.root, "linked.md"));
    assert.throws(() => a.store.create("valid", { ...a.input, sources: [{ path: "linked.md" }] }), expectCode("SYMLINK"));
    mkdirSync(join(a.root, ".pi"));
    symlinkSync(b.root, a.store.bundle);
    assert.throws(() => new ProjectMemory(a.root), expectCode("SYMLINK"));
  } finally { rmSync(a.root, { recursive: true, force: true }); rmSync(b.root, { recursive: true, force: true }); }
});

test("损坏记录不会被静默忽略，已有写锁时不修改内容", () => {
  const { root, input, store } = fixture();
  try {
    store.create("valid", input);
    writeFileSync(join(store.bundle, ".write.lock"), "");
    assert.throws(() => store.update("valid", { ...input, body: "错误变更" }), expectCode("BUSY"));
    rmSync(join(store.bundle, ".write.lock"));
    writeFileSync(join(store.bundle, "broken.md"), "not OKF");
    assert.throws(() => store.update("valid", { ...input, body: "错误变更" }), expectCode("INVALID_RECORD"));
    assert.equal(store.read("valid").body, input.body);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("读取旧来源已迁移的记忆仍可行，更新必须提供当前存在的来源", () => {
  const { root, input, store } = fixture();
  try {
    store.create("valid", input);
    renameSync(join(root, "testing.md"), join(root, "testing-new.md"));
    assert.equal(store.read("valid").sources[0].path, "testing.md");
    assert.throws(() => store.update("valid", input), expectCode("INVALID_SOURCE"));
    store.update("valid", { ...input, sources: [{ path: "testing-new.md" }] });
    const meta = store.read("valid").metadata;
    assert.match((meta.sources as {resource: string}[])[0].resource, /testing-new.md$/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("宽容读取未知 OKF 类型与字段，不强制可选索引/日志存在", () => {
  const { root, store } = fixture();
  try {
    mkdirSync(store.bundle, { recursive: true });
    writeFileSync(join(store.bundle, "external.md"), "---\ntype: Custom Type\nunknown: 42\n---\n\n外部内容\n");
    assert.equal(store.read("external").metadata.unknown, 42);
    assert.equal(store.search("外部").length, 1);
    const parsed = parse("type: Custom Type"); assert.equal(parsed.type, "Custom Type");
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("pi 适配层注册五个真实工具，使用 ctx.cwd 且错误抛给运行时", async () => {
  const { root, input } = fixture();
  const tools: ToolDefinition[] = [];
  extension({ registerTool(tool: ToolDefinition) { tools.push(tool); } } as unknown as ExtensionAPI);
  const ctx = { cwd: root } as Parameters<ToolDefinition["execute"]>[4];
  const invoke = (name: string, args: Record<string, unknown>) => tools.find(tool => tool.name === name)!.execute("test", args, undefined, undefined, ctx);
  try {
    assert.equal(tools.length, 5);
    const created = await invoke("project_memory_create", { id: "testing/session-entry", ...input });
    assert.equal((created.details as {id: string}).id, "testing/session-entry");
    const read = await invoke("project_memory_read", { id: "testing/session-entry" });
    assert.equal((read.details as {body: string}).body, input.body);
    await assert.rejects(invoke("project_memory_update", { id: "missing", ...input }), /NOT_FOUND/);
    assert.equal(((await invoke("project_memory_delete", { id: "testing/session-entry" })).details as {action: string}).action, "delete");
  } finally { rmSync(root, { recursive: true, force: true }); }
});

import { existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, realpathSync, renameSync, rmSync, openSync, closeSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";
import { randomUUID } from "node:crypto";
import { parse, stringify } from "yaml";

export interface SourceInput { path: string; title?: string }
export interface MemoryInput {
  type: string;
  title: string;
  description: string;
  body: string;
  sources: SourceInput[];
}
export interface MemoryRecord extends MemoryInput {
  id: string;
  path: string;
  metadata: Record<string, unknown>;
}
export class MemoryError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

/** 一个实例始终使用 pi 当前工作目录，不向上查找 Git 根目录。 */
export class ProjectMemory {
  project: string;
  bundle: string;
  constructor(cwd: string) {
    this.project = realpathSync(cwd);
    this.bundle = join(this.project, ".pi", "project-memory");
    this.checkPath(this.bundle);
  }

  private checkPath(path: string): void {
    const rel = relative(this.project, path);
    if (isAbsolute(rel) || rel === ".." || rel.startsWith(`..${sep}`)) {
      throw new MemoryError("PATH_ESCAPE", "路径超出当前项目");
    }
    let current = this.project;
    for (const part of rel.split(sep).filter(Boolean)) {
      current = join(current, part);
      // 即使链接指向项目内部也拒绝，避免记忆记录别名与写入位置不一致。
      if (existsSync(current) || (() => { try { return lstatSync(current).isSymbolicLink(); } catch { return false; } })()) {
        if (lstatSync(current).isSymbolicLink()) throw new MemoryError("SYMLINK", "记忆及来源路径不能经过符号链接");
      }
    }
  }

  private recordPath(id: string): string {
    if (typeof id !== "string" || id.length > 200 || !/^[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*$/.test(id)
      || id.split("/").some(part => ["index", "log"].includes(part.toLowerCase()))) {
      throw new MemoryError("INVALID_ID", "标识须为斜杠分隔的字母、数字、下划线或短横线，不含 .md；index/log 为保留名");
    }
    const path = join(this.bundle, `${id}.md`);
    this.checkPath(path);
    return path;
  }

  private validate(input: MemoryInput): void {
    for (const key of ["type", "title", "description", "body"] as const) {
      if (typeof input[key] !== "string" || !input[key].trim()) throw new MemoryError("INVALID_INPUT", `${key} 不能为空`);
    }
    if ([input.type, input.title, input.description].some(value => /[\r\n]/.test(value))) {
      throw new MemoryError("INVALID_INPUT", "type、title 和 description 须为单行");
    }
    if (input.body.length > 100_000) throw new MemoryError("INVALID_INPUT", "正文不能超过 100000 字符");
    if (!Array.isArray(input.sources) || input.sources.length < 1 || input.sources.length > 20) {
      throw new MemoryError("INVALID_INPUT", "须提供 1–20 条来源");
    }
    for (const source of input.sources) {
      if (!source || typeof source.path !== "string" || !source.path.trim() || isAbsolute(source.path)
        || source.path.split(/[\\/]/).includes("..")) throw new MemoryError("INVALID_SOURCE", "来源须为项目内相对文件路径");
      const path = resolve(this.project, source.path);
      this.checkPath(path);
      if (!existsSync(path) || !lstatSync(path).isFile()) throw new MemoryError("INVALID_SOURCE", `来源文件不存在：${source.path}`);
    }
  }

  read(id: string): MemoryRecord {
    const path = this.recordPath(id);
    if (!existsSync(path)) throw new MemoryError("NOT_FOUND", `记忆不存在：${id}`);
    const raw = readFileSync(path, "utf8");
    const match = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)([\s\S]*)$/.exec(raw);
    if (!match) throw new MemoryError("INVALID_RECORD", `记忆缺少 YAML 元信息：${id}`);
    const metadata: unknown = parse(match[1]);
    if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)
      || typeof (metadata as Record<string, unknown>).type !== "string"
      || !String((metadata as Record<string, unknown>).type).trim()) {
      throw new MemoryError("INVALID_RECORD", `记忆缺少非空 type：${id}`);
    }
    const meta = metadata as Record<string, unknown>;
    // 未知 OKF 字段与类型原样保留；读取不要求历史来源文件仍存在。
    return { id, path, type: String(meta.type), title: String(meta.title ?? id),
      description: String(meta.description ?? ""), body: match[2].trim(),
      sources: Array.isArray(meta.project_sources) ? meta.project_sources as SourceInput[] : [], metadata: meta };
  }

  private records(): MemoryRecord[] {
    this.checkPath(this.bundle);
    if (!existsSync(this.bundle)) return [];
    const ids: string[] = [];
    const walk = (dir: string): void => {
      for (const item of readdirSync(dir, { withFileTypes: true })) {
        if (item.name.startsWith(".")) continue;
        const path = join(dir, item.name);
        if (item.isSymbolicLink()) throw new MemoryError("SYMLINK", "记忆目录包含符号链接");
        if (item.isDirectory()) walk(path);
        else if (item.isFile() && item.name.endsWith(".md") && !["index.md", "log.md"].includes(item.name.toLowerCase())) {
          ids.push(relative(this.bundle, path).split(sep).join("/").slice(0, -3));
        }
      }
    };
    walk(this.bundle);
    return ids.sort().map(id => this.read(id));
  }

  search(query: string, limit = 10) {
    if (typeof query !== "string" || !query.trim() || !Number.isInteger(limit) || limit < 1 || limit > 50) {
      throw new MemoryError("INVALID_INPUT", "query 不能为空，limit 须为 1–50 整数");
    }
    // 空格分隔的词按 AND 匹配，不将用户输入解释成正则。
    const terms = query.toLowerCase().trim().split(/\s+/);
    return this.records().map(record => {
      const fields = [record.title, record.description, record.body].map(value => value.toLowerCase());
      const score = terms.every(term => fields.some(field => field.includes(term)))
        ? terms.reduce((sum, term) => sum + (fields[0].includes(term) ? 3 : fields[1].includes(term) ? 2 : 1), 0) : 0;
      return { record, score };
    }).filter(hit => hit.score > 0).sort((a, b) => b.score - a.score || a.record.id.localeCompare(b.record.id))
      .slice(0, limit).map(({ record, score }) => ({ id: record.id, title: record.title, description: record.description, path: record.path, score }));
  }

  create(id: string, input: MemoryInput) { return this.mutate("Create", id, input); }
  update(id: string, input: MemoryInput) { return this.mutate("Update", id, input); }
  delete(id: string) { return this.mutate("Delete", id); }

  private atomicWrite(path: string, text: string): void {
    this.checkPath(path);
    mkdirSync(dirname(path), { recursive: true });
    const temp = join(dirname(path), `.memory-${randomUUID()}.tmp`);
    try {
      writeFileSync(temp, text, { flag: "wx", mode: 0o600 });
      renameSync(temp, path);
    } finally { rmSync(temp, { force: true }); }
  }

  private mutate(action: "Create" | "Update" | "Delete", id: string, input?: MemoryInput) {
    const path = this.recordPath(id);
    if (input) this.validate(input);
    mkdirSync(this.bundle, { recursive: true });
    this.checkPath(this.bundle);
    const lock = join(this.bundle, ".write.lock");
    let fd: number;
    try { fd = openSync(lock, "wx", 0o600); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") throw new MemoryError("BUSY", "记忆正在写入；稍后重试。异常退出遗留锁需确认无写进程后手动移除");
      throw error;
    }
    const snapshots = new Map<string, string | undefined>();
    try {
      for (const target of [path, join(this.bundle, "index.md"), join(this.bundle, "log.md")]) {
        this.checkPath(target);
        snapshots.set(target, existsSync(target) ? readFileSync(target, "utf8") : undefined);
      }
      const exists = existsSync(path);
      if (action === "Create" && exists) throw new MemoryError("ALREADY_EXISTS", `记忆已存在，请使用 update：${id}`);
      if (action !== "Create" && !exists) throw new MemoryError("NOT_FOUND", `记忆不存在，未新增：${id}`);
      // 同时检查其他记录有效，避免修改后才发现索引无法重建。
      this.records();
      if (action === "Delete") rmSync(path);
      else if (input) {
        const oldMeta = action === "Update" ? this.read(id).metadata : {};
        const metadata = { ...oldMeta, type: input.type, title: input.title, description: input.description,
          sources: input.sources.map((source, i) => ({ id: `source-${i + 1}`, resource: pathToFileURL(resolve(this.project, source.path)).href, title: source.title ?? source.path })),
          project_sources: input.sources,
          generated: { by: "week04-project-memory/0.1.0", at: new Date().toISOString() } };
        this.atomicWrite(path, `---\n${stringify(metadata).trimEnd()}\n---\n\n${input.body.trim()}\n`);
      }
      const records = this.records();
      const groups = new Map<string, MemoryRecord[]>();
      for (const record of records) groups.set(record.type, [...(groups.get(record.type) ?? []), record]);
      const index = [...groups].sort(([a], [b]) => a.localeCompare(b)).map(([type, items]) =>
        `## ${type}\n\n${items.map(item => `- [${item.title}](/${item.id}.md) — ${item.description}`).join("\n")}`).join("\n\n");
      this.atomicWrite(join(this.bundle, "index.md"), `---\nokf_version: "0.2"\n---\n\n# 项目长期记忆\n\n${index || "暂无记忆。"}\n`);
      const logPath = join(this.bundle, "log.md");
      const before = existsSync(logPath) ? readFileSync(logPath, "utf8") : "# 记忆变更日志\n";
      const date = new Date().toISOString().slice(0, 10);
      const entry = `- **${action}** \`${id}\` · ${new Date().toISOString()}`;
      const group = `## ${date}`;
      const next = before.includes(`${group}\n`)
        ? before.replace(`${group}\n`, `${group}\n\n${entry}\n`)
        : before.replace(/^# [^\n]*\n/, `$&\n${group}\n\n${entry}\n`);
      this.atomicWrite(logPath, next.replace(/\n{3,}/g, "\n\n"));
      return { action: action.toLowerCase(), id, path, project: this.project, bundle: this.bundle };
    } catch (error) {
      // 普通 I/O 失败时恢复记录、索引和日志；不声称支持进程崩溃事务。
      for (const [target, text] of snapshots) {
        if (text === undefined) rmSync(target, { force: true });
        else if (!existsSync(target) || readFileSync(target, "utf8") !== text) this.atomicWrite(target, text);
      }
      throw error;
    } finally { closeSync(fd); rmSync(lock, { force: true }); }
  }
}

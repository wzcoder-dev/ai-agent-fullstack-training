import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { MemoryError, ProjectMemory } from "./store.ts";

const id = Type.String({ minLength: 1, maxLength: 200, description: "稳定记忆标识，例如 testing/session-entry；不是测试文件路径，不含 .md" });
const fields = {
  type: Type.String({ minLength: 1, description: "OKF 概念类型，例如 Playbook" }),
  title: Type.String({ minLength: 1, description: "单行标题" }),
  description: Type.String({ minLength: 1, description: "单行摘要" }),
  body: Type.String({ minLength: 1, maxLength: 100000, description: "当前事实的完整 Markdown 正文，包含测试路径、命令和实际运行结果；更新时替换旧正文" }),
  sources: Type.Array(Type.Object({ path: Type.String({ minLength: 1, description: "已读取、实际存在的项目内相对文件路径" }), title: Type.Optional(Type.String()) }, { additionalProperties: false }), { minItems: 1, maxItems: 20, description: "内容依据，如测试文件、测试说明或保存的执行日志" }),
};
function result(operation: () => unknown) {
  try {
    const data = operation();
    return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }], details: data };
  } catch (error) {
    const data = { code: error instanceof MemoryError ? error.code : "IO_ERROR", message: error instanceof Error ? error.message : String(error) };
    // pi 通过抛出的异常标记工具执行失败；不能只在普通结果中附加 isError。
    throw new Error(JSON.stringify(data));
  }
}
export default function projectMemory(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "project_memory_create", label: "创建项目记忆",
    description: "创建当前 pi 工作目录所属项目的 OKF 长期记忆，附文件来源，返回实际保存路径。已存在时报错，不覆盖。",
    promptSnippet: "创建带来源的项目长期记忆，返回实际保存路径",
    promptGuidelines: ["记忆只保存实际读到的项目事实。读取测试和说明后再保存；运行结果须据实填写，未执行不得声称通过。"],
    parameters: Type.Object({ id, ...fields }, { additionalProperties: false }), executionMode: "sequential",
    async execute(_call, params, _signal, _update, ctx) { return result(() => new ProjectMemory(ctx.cwd).create(params.id, params)); },
  });
  pi.registerTool({
    name: "project_memory_search", label: "查找项目记忆",
    description: "仅在当前 pi 工作目录的 .pi/project-memory 中按关键词搜索标题、摘要、正文；返回标识和摘要，随后按标识读取全文。多词以空格分隔，全部匹配。",
    promptSnippet: "按关键词查找当前项目长期记忆",
    promptGuidelines: ["用户要求从长期记忆查找时先 search，再 read，不以会话历史代替存储读取；无命中时明确说明。"],
    parameters: Type.Object({ query: Type.String({ minLength: 1 }), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 50, default: 10 })) }, { additionalProperties: false }),
    async execute(_call, params, _signal, _update, ctx) { return result(() => ({ project: ctx.cwd, matches: new ProjectMemory(ctx.cwd).search(params.query, params.limit) })); },
  });
  pi.registerTool({
    name: "project_memory_read", label: "读取项目记忆",
    description: "按稳定标识读取当前项目记忆全文、元信息和路径。记忆可能过时，读取测试入口后须核对当前文件存在。",
    promptSnippet: "按标识读取项目记忆全文及来源",
    parameters: Type.Object({ id }, { additionalProperties: false }),
    async execute(_call, params, _signal, _update, ctx) { return result(() => new ProjectMemory(ctx.cwd).read(params.id)); },
  });
  pi.registerTool({
    name: "project_memory_update", label: "更新项目记忆",
    description: "按原标识完整替换当前项目已有记忆的正文、摘要和来源，不合并旧正文。找不到时报错，不新增。先读取原记录；迁移并运行新测试后按实际结果更新。",
    promptSnippet: "按原标识更新已有记忆，找不到不新增",
    parameters: Type.Object({ id, ...fields }, { additionalProperties: false }), executionMode: "sequential",
    async execute(_call, params, _signal, _update, ctx) { return result(() => new ProjectMemory(ctx.cwd).update(params.id, params)); },
  });
  pi.registerTool({
    name: "project_memory_delete", label: "删除项目记忆",
    description: "按标识删除当前项目的记忆文件，同步索引并记录删除动作。删除后不能再搜索或读取该记录；不存在时报错。",
    promptSnippet: "删除项目记忆并同步索引和日志",
    parameters: Type.Object({ id }, { additionalProperties: false }), executionMode: "sequential",
    async execute(_call, params, _signal, _update, ctx) { return result(() => new ProjectMemory(ctx.cwd).delete(params.id)); },
  });
}

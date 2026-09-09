"""输入层：仓库摘要扫描、只读文件工具与 token 预算。

作业要求的输入三要素在这里落地：
- 问题描述：由调用方传入，不经本模块；
- 仓库摘要：scan_repo 自动扫描（目录树、依赖、现有测试）；
- 相关文件片段：模型通过 ToolHub 的 list_files / read_file 自己收集，
  受字符预算约束（1 token ≈ 3~4 字符英文、≈ 1~1.5 字符中文，
  这里直接用字符数做预算单位，够用且可解释）。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULT_CONTEXT_BUDGET_CHARS = 24_000
DEFAULT_PER_READ_CAP_CHARS = 6_000
DEFAULT_LIST_FILES_CAP = 200

IGNORED_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode", "node_modules", "dist", "build",
}


def _is_ignored_dir(name: str) -> bool:
    return name in IGNORED_DIR_NAMES or name.endswith(".egg-info")


def _walk_files(root: Path) -> list[Path]:
    """按排序返回仓库内所有普通文件（相对路径），跳过忽略目录与点文件。"""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _is_ignored_dir(d) and not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            found.append(Path(dirpath) / name)
    return found


def _read_dependencies(root: Path) -> list[str]:
    """尽力解析 pyproject.toml 的主依赖，解析失败就当没有。"""
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        deps = data["project"]["dependencies"]
        return [str(dep) for dep in deps]
    except (OSError, KeyError, tomllib.TOMLDecodeError, TypeError):
        return []


def _test_files(files: list[Path]) -> list[Path]:
    return [f for f in files if f.name.startswith("test_") or f.name.endswith("_test.py")]


def scan_repo(root: Path, *, max_entries: int = DEFAULT_LIST_FILES_CAP) -> str:
    """生成仓库摘要文本：目录结构、依赖、现有测试。"""
    root = root.resolve()
    files = _walk_files(root)

    lines: list[str] = ["[目录结构]"]
    shown = files[:max_entries]
    lines += [str(f.relative_to(root)) for f in shown]
    if len(files) > len(shown):
        lines.append(f"...（其余 {len(files) - len(shown)} 个文件省略，可用 list_files 工具查看子目录）")

    deps = _read_dependencies(root)
    lines.append("")
    lines.append("[依赖] (pyproject.toml)" if deps else "[依赖] 未找到 pyproject.toml")
    lines += [f"- {dep}" for dep in deps]

    tests = _test_files(files)
    lines.append("")
    lines.append("[现有测试]" if tests else "[现有测试] 未发现测试文件")
    lines += [str(f.relative_to(root)) for f in tests]

    return "\n".join(lines)


class ToolError(Exception):
    """工具级失败（路径越界、未知工具等）。会作为错误结果回传给模型，不终止 Loop。"""


class ToolHub:
    """list_files / read_file 两个只读工具，带路径安全与字符预算。

    所有路径先 resolve 再与仓库根比较，符号链接逃逸也会被拦下；
    成功读取的内容计入上下文预算，超出后工具返回提示而不是报错，
    让模型自行决定基于已有信息收尾。
    """

    def __init__(
        self,
        root: Path,
        *,
        context_budget: int = DEFAULT_CONTEXT_BUDGET_CHARS,
        per_read_cap: int = DEFAULT_PER_READ_CAP_CHARS,
        list_files_cap: int = DEFAULT_LIST_FILES_CAP,
    ) -> None:
        self.root = root.resolve()
        self.context_budget = context_budget
        self.per_read_cap = per_read_cap
        self.list_files_cap = list_files_cap
        self.used_chars = 0

    def run(self, name: str, arguments: dict) -> str:
        handlers = {"list_files": self.list_files, "read_file": self.read_file}
        if name not in handlers:
            raise ToolError(
                f"未知工具 {name!r}，可用工具：{', '.join(sorted(handlers))}"
            )
        result = handlers[name](arguments)
        self.used_chars += len(result)
        return result

    def _resolve(self, rel: str) -> Path:
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("path 必须是仓库内相对路径字符串")
        candidate = (self.root / rel).resolve()
        if not candidate.is_relative_to(self.root):
            raise ToolError(f"路径越出仓库范围: {rel!r}")
        return candidate

    def list_files(self, arguments: dict) -> str:
        subdir = arguments.get("subdir") or "."
        target = self._resolve(str(subdir))
        if not target.is_dir():
            raise ToolError(f"不是目录: {subdir!r}")
        files = [f for f in _walk_files(target) if f.is_relative_to(self.root)]
        rels = [str(f.relative_to(self.root)) for f in files]
        lines = rels[: self.list_files_cap]
        if len(rels) > len(lines):
            lines.append(f"...（共 {len(rels)} 个文件，仅列出前 {len(lines)} 个）")
        header = f"[{target.relative_to(self.root) or '.'}] 共 {len(rels)} 个文件"
        return "\n".join([header, *lines])

    def read_file(self, arguments: dict) -> str:
        path = self._resolve(str(arguments.get("path", "")))
        if not path.is_file():
            raise ToolError(f"文件不存在: {arguments.get('path')!r}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(int(arguments.get("start_line", 1) or 1), 1)
        end = int(arguments.get("end_line", len(lines)) or len(lines))
        window = lines[start - 1 : end]
        if not window:
            raise ToolError(f"行号区间为空: start_line={start}, end_line={end}")

        width = len(str(start + len(window)))
        numbered = [f"{no:>{width}} | {text}" for no, text in enumerate(window, start=start)]

        kept: list[str] = []
        total = 0
        truncated_by_cap = False
        for line in numbered:
            if total + len(line) > self.per_read_cap:
                truncated_by_cap = True
                break
            kept.append(line)
            total += len(line)

        header = f"{path.relative_to(self.root)} 第 {start}-{start + len(kept) - 1} 行（共 {len(lines)} 行）"
        parts = [header, *kept]
        if truncated_by_cap:
            parts.append(
                f"...（单次读取上限 {self.per_read_cap} 字符，可用 start_line/end_line 分段继续读）"
            )
        if self.used_chars + total > self.context_budget:
            return (
                f"上下文预算已用完（已读 {self.used_chars}/{self.context_budget} 字符）。"
                "请基于已有信息直接输出最终计划 JSON。"
            )
        return "\n".join(parts)

"""CLI 入口：输入一个代码问题，输出一个可校验的修改计划。

用法示例：

    export DEEPSEEK_API_KEY=sk-xxx
    uv run python main.py --issue "sandbox_runner.py 没有超时保护，请规划修复"
    uv run python main.py --issue-file issue.txt --provider zhipu
    uv run python main.py --issue "..." --provider fake   # 离线演示

失败时退出码为 1，trace 文件路径会打印出来，用于定位失败原因。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from plan_agent.adapters import build_adapter
from plan_agent.context import ToolHub
from plan_agent.loop import MAX_TOOL_ROUNDS_DEFAULT, LoopResult, run_planner
from plan_agent.trace import Trace

PROJECT_ROOT = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    """极简 .env 加载：只补充进程环境里缺失的变量，不覆盖已有值。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def find_default_repo() -> Path:
    """默认分析对象：本仓库的 course_code/week01/1-1（从 .git 根定位）。"""
    for parent in [PROJECT_ROOT, *PROJECT_ROOT.parents]:
        if (parent / ".git").exists():
            candidate = parent / "course_code" / "week01" / "1-1"
            return candidate if candidate.is_dir() else parent
    return Path.cwd()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mini Coding Agent：输出可校验的修改计划")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--issue", help="代码问题描述（git issue 或用户提问）")
    source.add_argument("--issue-file", help="从文件读取问题描述")
    parser.add_argument("--repo", type=Path, default=None, help="被分析的仓库根目录（默认 course_code/week01/1-1）")
    parser.add_argument("--provider", choices=["deepseek", "zhipu"], default="deepseek", help="模型供应商")
    parser.add_argument("--model", default=None, help="覆盖默认模型名")
    parser.add_argument("--max-tool-rounds", type=int, default=MAX_TOOL_ROUNDS_DEFAULT)
    parser.add_argument("--trace-out", type=Path, default=None, help="trace 输出路径（默认 traces/ 下按时间命名）")
    return parser.parse_args(argv)


def save_trace(result: LoopResult, trace_out: Path | None) -> Path:
    path = trace_out or PROJECT_ROOT / "traces" / f"trace-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return result.trace.save(path)


def main(argv: list[str] | None = None) -> int:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_args(argv)

    issue = args.issue or Path(args.issue_file).read_text(encoding="utf-8").strip()
    repo = (args.repo or find_default_repo()).resolve()
    if not repo.is_dir():
        print(f"仓库目录不存在: {repo}", file=sys.stderr)
        return 2

    try:
        adapter = build_adapter(args.provider, model=args.model)
    except RuntimeError as exc:
        # 缺少 API key 等配置问题：给出明确指引，不甩堆栈
        print(str(exc), file=sys.stderr)
        return 2

    print(f"[agent_start] provider={adapter.name} model={getattr(adapter, 'model', '?')} repo={repo}")
    result = run_planner(
        issue=issue,
        repo_root=repo,
        adapter=adapter,
        tools=ToolHub(repo),
        max_tool_rounds=args.max_tool_rounds,
        trace=Trace(),
    )

    trace_path = save_trace(result, args.trace_out)
    print(result.render())
    print(f"\n[agent_end] status={result.status}")
    print(f"trace: {trace_path}")
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

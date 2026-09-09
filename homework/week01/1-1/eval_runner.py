"""回归评测：跑 cases/eval_cases.json，校验输出契约与期望，输出 pass rate。

对应验收要求「同类请求可回归」：
- 每个用例 = 一条 issue + 结构化期望（action / file_path / reason / risk）；
- 每次运行都会把 trace 落到 traces/eval/ 下，失败用例可直接定位；
- --provider fake 时用用例里自带的 fake_responses 脚本离线回归，不需要 API key。

用法：

    uv run python eval_runner.py --provider fake
    export DEEPSEEK_API_KEY=sk-xxx && uv run python eval_runner.py
    uv run python eval_runner.py --provider zhipu
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from plan_agent.adapters import FakeAdapter, ModelAdapter, build_adapter
from plan_agent.context import ToolHub
from plan_agent.loop import run_planner
from plan_agent.schema import PlanContract
from plan_agent.trace import Trace

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CASES = PROJECT_ROOT / "cases" / "eval_cases.json"


def load_cases(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def judge(plan: PlanContract, expect: dict) -> list[str]:
    """返回不满足期望的问题列表；空列表 = 通过。契约本身已由 schema 强校验。"""
    problems: list[str] = []
    if "action" in expect and plan.action != expect["action"]:
        problems.append(f"action 期望 {expect['action']}，实际 {plan.action}")
    if "risk_in" in expect and plan.risk not in expect["risk_in"]:
        problems.append(f"risk 期望 ∈ {expect['risk_in']}，实际 {plan.risk}")
    if "file_path_contains" in expect:
        for token in expect["file_path_contains"]:
            if not plan.file_path or token not in plan.file_path:
                problems.append(f"file_path 应包含 {token!r}，实际 {plan.file_path!r}")
    if "reason_contains" in expect:
        for token in expect["reason_contains"]:
            if token not in plan.reason:
                problems.append(f"reason 应包含 {token!r}")
    return problems


def find_default_repo() -> Path:
    for parent in [PROJECT_ROOT, *PROJECT_ROOT.parents]:
        if (parent / ".git").exists():
            candidate = parent / "course_code" / "week01" / "1-1"
            return candidate if candidate.is_dir() else parent
    return Path.cwd()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mini Coding Agent 回归评测")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--repo", type=Path, default=None, help="被分析仓库（默认 course_code/week01/1-1）")
    parser.add_argument("--provider", choices=["deepseek", "zhipu", "fake"], default="deepseek")
    parser.add_argument("--model", default=None)
    parser.add_argument("--traces-dir", type=Path, default=PROJECT_ROOT / "traces" / "eval")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases = load_cases(args.cases)
    repo = (args.repo or find_default_repo()).resolve()

    adapter: ModelAdapter
    if args.provider == "fake":
        # 离线回归：每个用例用自己的脚本化响应，行为完全确定
        for case in cases:
            if "fake_responses" not in case:
                print(f"用例 {case.get('name')} 缺少 fake_responses，无法离线运行", file=sys.stderr)
                return 2
    else:
        adapter = build_adapter(args.provider, model=args.model)

    passed = 0
    for idx, case in enumerate(cases, start=1):
        if args.provider == "fake":
            adapter = FakeAdapter(case["fake_responses"])
        result = run_planner(
            issue=case["issue"],
            repo_root=repo,
            adapter=adapter,
            tools=ToolHub(repo),
            trace=Trace(
                meta={"case": case["name"], "provider": args.provider, "model": getattr(adapter, "model", "?")}
            ),
        )

        problems: list[str]
        if result.status != "ok" or result.plan is None:
            problems = [f"运行失败: {(result.failure or {}).get('kind')} {(result.failure or {}).get('message')}"]
        else:
            problems = judge(result.plan, case.get("expect", {}))

        ok = not problems
        passed += ok
        plan = result.plan
        summary = (
            f"action={getattr(plan, 'action', '-')} file_path={getattr(plan, 'file_path', '-')} "
            f"risk={getattr(plan, 'risk', '-')}"
        )
        print(f"[case {idx}] name={case['name']} ok={ok} {summary}")
        if problems:
            for problem in problems:
                print(f"  ✗ {problem}")
            trace_path = args.traces_dir / f"{idx:02d}-{case['name']}.json"
            result.trace.save(trace_path)
            print(f"  trace: {trace_path}")

    print(f"\nPass rate: {passed}/{len(cases)}  (provider={args.provider}, repo={repo})")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())

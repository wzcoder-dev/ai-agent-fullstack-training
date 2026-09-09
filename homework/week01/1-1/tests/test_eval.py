"""回归评测判定逻辑与用例文件本身的单元测试。"""

import json
from pathlib import Path

from eval_runner import DEFAULT_CASES, judge, load_cases
from plan_agent.loop import extract_json_object
from plan_agent.schema import PlanContract

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def make_plan(**overrides) -> PlanContract:
    base = {
        "action": "edit",
        "file_path": "plan_agent/core.py",
        "reason": "需要补充超时保护，避免卡死。",
        "risk": "low",
    }
    base.update(overrides)
    return PlanContract.model_validate(base)


def test_judge_passes_on_match():
    expect = {
        "action": "edit",
        "file_path_contains": ["core.py"],
        "reason_contains": ["超时"],
        "risk_in": ["low", "medium"],
    }
    assert judge(make_plan(), expect) == []


def test_judge_reports_each_mismatch():
    expect = {
        "action": "finish",
        "file_path_contains": ["other.py"],
        "reason_contains": ["不存在的关键词"],
        "risk_in": ["high"],
    }
    problems = judge(make_plan(), expect)
    assert any("action" in p for p in problems)
    assert any("file_path" in p for p in problems)
    assert any("reason" in p for p in problems)
    assert any("risk" in p for p in problems)


def test_judge_handles_missing_file_path():
    expect = {"file_path_contains": ["core.py"]}
    problems = judge(make_plan(action="finish", file_path=None), expect)
    assert len(problems) == 1


def test_case_file_is_well_formed():
    """用例文件本身也要可校验：字段齐全、fake 脚本的最终响应必须是合法契约计划。"""
    cases: list[dict] = load_cases(DEFAULT_CASES)
    assert len(cases) >= 3
    for case in cases:
        assert case["name"] and case["issue"]
        assert isinstance(case.get("expect"), dict)
        responses = case.get("fake_responses")
        assert isinstance(responses, list) and responses
        final = extract_json_object(responses[-1])
        assert final is not None, f"用例 {case['name']} 的最终 fake 响应不是 JSON 对象"
        PlanContract.model_validate(final)  # 不合法会抛 ValidationError


def test_case_issues_are_distinct():
    cases = load_cases(DEFAULT_CASES)
    names = [case["name"] for case in cases]
    assert len(names) == len(set(names))
    raw = json.loads(DEFAULT_CASES.read_text(encoding="utf-8"))
    assert [c["name"] for c in raw] == names

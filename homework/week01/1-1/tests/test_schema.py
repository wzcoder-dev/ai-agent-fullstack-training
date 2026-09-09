"""输出契约的单元测试：合法计划放行，非法输入逐字段报错。"""

import pytest
from pydantic import ValidationError

from plan_agent.schema import PlanContract, format_validation_errors, render_plan


def make_plan(**overrides) -> dict:
    base = {"action": "finish", "file_path": None, "reason": "问题不需要代码修改。", "risk": "low"}
    return {**base, **overrides}


def test_valid_plan_passes():
    plan = PlanContract.model_validate(make_plan())
    assert plan.action == "finish"
    assert plan.file_path is None


def test_valid_edit_plan_passes():
    plan = PlanContract.model_validate(make_plan(action="edit", file_path="plan_agent/loop.py", risk="medium"))
    assert plan.file_path == "plan_agent/loop.py"


def test_edit_without_file_path_rejected():
    with pytest.raises(ValidationError) as exc_info:
        PlanContract.model_validate(make_plan(action="edit"))
    assert "file_path" in format_validation_errors(exc_info.value)


def test_absolute_path_rejected():
    with pytest.raises(ValidationError) as exc_info:
        PlanContract.model_validate(make_plan(action="edit", file_path="/etc/passwd"))
    assert "相对路径" in format_validation_errors(exc_info.value)


def test_dotdot_escape_rejected():
    with pytest.raises(ValidationError) as exc_info:
        PlanContract.model_validate(make_plan(action="edit", file_path="plan_agent/../../etc/passwd"))
    assert ".." in format_validation_errors(exc_info.value)


def test_backslash_path_rejected():
    with pytest.raises(ValidationError):
        PlanContract.model_validate(make_plan(action="edit", file_path="plan_agent\\loop.py"))


def test_empty_file_path_rejected():
    with pytest.raises(ValidationError):
        PlanContract.model_validate(make_plan(file_path="   "))


def test_reason_length_bounds():
    with pytest.raises(ValidationError):
        PlanContract.model_validate(make_plan(reason="   "))
    with pytest.raises(ValidationError) as exc_info:
        PlanContract.model_validate(make_plan(reason="字" * 501))
    assert "reason" in format_validation_errors(exc_info.value)
    # 恰好 500 字应放行
    PlanContract.model_validate(make_plan(reason="字" * 500))


def test_invalid_enum_rejected():
    with pytest.raises(ValidationError) as exc_info:
        PlanContract.model_validate(make_plan(action="delete"))
    assert "action" in format_validation_errors(exc_info.value)
    with pytest.raises(ValidationError) as exc_info:
        PlanContract.model_validate(make_plan(risk="extreme"))
    assert "risk" in format_validation_errors(exc_info.value)


def test_extra_fields_ignored():
    """模型多输出无害字段不触发重试（契约外的附加信息直接忽略）。"""
    payload = make_plan()
    payload["summary"] = "模型自作主张多带的字段"
    plan = PlanContract.model_validate(payload)
    assert plan.action == "finish"


def test_missing_required_field_reports_location():
    with pytest.raises(ValidationError) as exc_info:
        PlanContract.model_validate({"action": "edit", "file_path": "a.py", "risk": "low"})
    text = format_validation_errors(exc_info.value)
    assert "reason" in text


def test_render_plan_contains_all_fields():
    plan = PlanContract.model_validate(make_plan(action="edit", file_path="a.py", risk="high", reason="改动核心链路。"))
    text = render_plan(plan)
    assert "action: edit" in text
    assert "file_path: a.py" in text
    assert "risk: high" in text
    assert "改动核心链路。" in text

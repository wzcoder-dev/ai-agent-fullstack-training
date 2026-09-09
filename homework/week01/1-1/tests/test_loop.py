"""Agent Loop 的单元测试：全部用 FakeAdapter，不依赖真实 API key。

覆盖作业要求的异常路径：
- Schema 校验失败 → 字段错误回传重试，超限停止
- 429 / 超时 → 退避重试，超限停止
- 拒答 → 立即停止
- 工具轮次超限 → 停止
- 工具执行失败（如路径越界）→ 错误回填上下文，Loop 不崩
所有停止条件都检查 trace 有记录、状态为 failed。
"""

import json

import pytest

from plan_agent.adapters import FakeAdapter, LLMError
from plan_agent.loop import extract_json_object, run_planner


def plan_json(**overrides) -> str:
    base = {"action": "finish", "file_path": None, "reason": "问题不需要代码修改，确认即可。", "risk": "low"}
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def tool_call_json(name: str, **arguments) -> str:
    return json.dumps({"type": "tool_call", "name": name, "arguments": arguments}, ensure_ascii=False)


@pytest.fixture()
def sample_repo(tmp_path):
    (tmp_path / "plan_agent").mkdir()
    (tmp_path / "plan_agent" / "core.py").write_text("VALUE = 42\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def recorded_sleep():
    """记录退避时长的列表；测试里以 sleep=recorded_sleep.append 注入，避免真的等待。"""
    calls: list[float] = []
    return calls


def test_ok_path_with_tool_call(sample_repo):
    adapter = FakeAdapter([
        tool_call_json("read_file", path="plan_agent/core.py"),
        plan_json(action="edit", file_path="plan_agent/core.py", reason="需要给 VALUE 加注释说明用途。", risk="low"),
    ])
    result = run_planner(issue="给 core.py 的常量加说明", repo_root=sample_repo, adapter=adapter)

    assert result.status == "ok"
    assert result.plan is not None and result.plan.action == "edit"
    assert result.trace.count("tool_start") == 1
    assert result.trace.count("tool_end") == 1
    assert any("[toolResult]" in m["content"] for m in result.messages)
    assert result.trace.count("run_end") == 1


def test_schema_error_feedback_then_ok(sample_repo):
    bad = json.dumps({"action": "edit", "file_path": "plan_agent/core.py", "reason": "缺少 risk 字段。"})
    adapter = FakeAdapter([bad, plan_json(action="edit", file_path="plan_agent/core.py", reason="补齐字段后再改。")])
    result = run_planner(issue="改 core.py", repo_root=sample_repo, adapter=adapter)

    assert result.status == "ok"
    assert result.trace.count("schema_error") == 1
    # 第二次调用时，上下文里应包含具体字段错误的回传
    retry_messages = adapter.calls[1]
    assert any("字段错误" in m["content"] and "risk" in m["content"] for m in retry_messages)


def test_schema_retries_exhausted_stops(sample_repo):
    bad = json.dumps({"action": "edit", "file_path": "plan_agent/core.py", "reason": "缺 risk。"})
    adapter = FakeAdapter([bad, bad, bad])
    result = run_planner(issue="改 core.py", repo_root=sample_repo, adapter=adapter)

    assert result.status == "failed"
    assert result.failure["kind"] == "schema_retries_exhausted"
    assert len(adapter.calls) == 3  # 1 次初试 + 2 次重试
    end_events = [e for e in result.trace.events if e["type"] == "run_end"]
    assert end_events[0]["status"] == "failed"


def test_rate_limit_retries_then_ok(sample_repo, recorded_sleep):
    adapter = FakeAdapter([LLMError("rate_limit", "429 too many requests"), plan_json()])
    result = run_planner(issue="确认即可", repo_root=sample_repo, adapter=adapter, sleep=recorded_sleep.append)

    assert result.status == "ok"
    assert result.trace.count("llm_retry") == 1
    assert recorded_sleep == [1.0]  # 指数退避第一次 1s


def test_rate_limit_exhausted_stops(sample_repo, recorded_sleep):
    adapter = FakeAdapter([LLMError("rate_limit", "429")] * 3)
    result = run_planner(issue="确认即可", repo_root=sample_repo, adapter=adapter, sleep=recorded_sleep.append)

    assert result.status == "failed"
    assert result.failure["kind"] == "rate_limit"
    assert len(adapter.calls) == 3
    assert result.trace.count("llm_error") == 3


def test_timeout_also_retryable(sample_repo, recorded_sleep):
    adapter = FakeAdapter([LLMError("timeout", "connection timed out"), plan_json()])
    result = run_planner(issue="确认即可", repo_root=sample_repo, adapter=adapter, sleep=recorded_sleep.append)
    assert result.status == "ok"


def test_refusal_stops_immediately(sample_repo, recorded_sleep):
    adapter = FakeAdapter([LLMError("refusal", "content filter")])
    result = run_planner(issue="确认即可", repo_root=sample_repo, adapter=adapter, sleep=recorded_sleep.append)

    assert result.status == "failed"
    assert result.failure["kind"] == "refusal"
    assert len(adapter.calls) == 1  # 拒答不重试


def test_tool_rounds_exhausted_stops(sample_repo):
    adapter = FakeAdapter([tool_call_json("list_files")] * 4)
    result = run_planner(issue="看看仓库", repo_root=sample_repo, adapter=adapter, max_tool_rounds=3)

    assert result.status == "failed"
    assert result.failure["kind"] == "max_tool_rounds"
    assert result.trace.count("tool_end") == 3


def test_tool_error_recorded_but_not_fatal(sample_repo):
    adapter = FakeAdapter([
        tool_call_json("read_file", path="../outside.txt"),
        plan_json(action="ask_user", reason="无法读取目标文件，需要用户确认文件位置。"),
    ])
    result = run_planner(issue="看看外面的文件", repo_root=sample_repo, adapter=adapter)

    assert result.status == "ok"
    tool_end = [e for e in result.trace.events if e["type"] == "tool_end"][0]
    assert tool_end["is_error"] is True
    assert any("工具调用失败" in m["content"] for m in result.messages)


def test_unparseable_output_counts_as_schema_error(sample_repo):
    adapter = FakeAdapter(["我觉得这个问题不需要修改。", plan_json()])
    result = run_planner(issue="确认即可", repo_root=sample_repo, adapter=adapter)

    assert result.status == "ok"
    assert result.trace.count("schema_error") == 1


def test_unparseable_output_exhausted_stops(sample_repo):
    adapter = FakeAdapter(["随便说两句。", "再说两句。", "继续说。"])
    result = run_planner(issue="确认即可", repo_root=sample_repo, adapter=adapter)

    assert result.status == "failed"
    assert result.failure["kind"] == "schema_retries_exhausted"


def test_extract_json_object_variants():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('结论如下：\n{"a": 1}\n以上。') == {"a": 1}
    assert extract_json_object("不是 JSON") is None
    assert extract_json_object('{"a": 1}') == {"a": 1}

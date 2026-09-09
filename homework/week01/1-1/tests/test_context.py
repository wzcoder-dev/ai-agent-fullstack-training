"""输入层的单元测试：仓库摘要扫描、路径安全、预算控制。"""

import pytest

from plan_agent.context import ToolError, ToolHub, scan_repo


@pytest.fixture()
def sample_repo(tmp_path):
    """一个最小的样例仓库：有依赖声明、源码、测试与应被忽略的目录。

    建在 tmp_path/repo 子目录下，留出 tmp_path 根目录放「仓库外」文件。
    """
    repo = tmp_path / "repo"
    (repo / "plan_agent").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "__pycache__").mkdir()
    (repo / ".venv").mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = ["openai>=1.30", "pydantic>=2.6"]\n',
        encoding="utf-8",
    )
    (repo / "plan_agent" / "core.py").write_text("VALUE = 42\n", encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    (repo / "__pycache__" / "core.pyc").write_text("junk", encoding="utf-8")
    (repo / ".venv" / "lib.py").write_text("junk", encoding="utf-8")
    return repo


def test_scan_repo_lists_tree_deps_and_tests(sample_repo):
    summary = scan_repo(sample_repo)
    assert "plan_agent/core.py" in summary
    assert "tests/test_core.py" in summary
    assert "openai>=1.30" in summary
    assert "[现有测试]" in summary
    assert "__pycache__" not in summary
    assert ".venv" not in summary


def test_read_file_returns_numbered_lines(sample_repo):
    hub = ToolHub(sample_repo)
    result = hub.run("read_file", {"path": "plan_agent/core.py"})
    assert "1 | VALUE = 42" in result
    assert hub.used_chars > 0


def test_read_file_window(sample_repo):
    (sample_repo / "plan_agent" / "big.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 21)), encoding="utf-8"
    )
    hub = ToolHub(sample_repo)
    result = hub.run("read_file", {"path": "plan_agent/big.py", "start_line": 5, "end_line": 7})
    assert "5 | line 5" in result
    assert "7 | line 7" in result
    assert "line 8" not in result


def test_read_file_respects_per_read_cap(sample_repo):
    (sample_repo / "plan_agent" / "long.txt").write_text("x" * 5000, encoding="utf-8")
    hub = ToolHub(sample_repo, per_read_cap=100)
    result = hub.run("read_file", {"path": "plan_agent/long.txt"})
    assert "单次读取上限" in result


def test_read_file_stops_at_context_budget(sample_repo):
    hub = ToolHub(sample_repo, context_budget=50, per_read_cap=40)
    first = hub.run("read_file", {"path": "plan_agent/core.py"})
    assert "预算" not in first
    second = hub.run("read_file", {"path": "plan_agent/core.py"})
    assert "预算已用完" in second


def test_path_escape_rejected(sample_repo):
    hub = ToolHub(sample_repo)
    with pytest.raises(ToolError):
        hub.run("read_file", {"path": "../outside.txt"})
    with pytest.raises(ToolError):
        hub.run("read_file", {"path": "/etc/passwd"})


def test_symlink_escape_rejected(sample_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (sample_repo / "leak.py").symlink_to(outside)
    hub = ToolHub(sample_repo)
    with pytest.raises(ToolError):
        hub.run("read_file", {"path": "leak.py"})


def test_unknown_tool_rejected(sample_repo):
    hub = ToolHub(sample_repo)
    with pytest.raises(ToolError) as exc_info:
        hub.run("write_file", {})
    assert "未知工具" in str(exc_info.value)


def test_list_files_with_subdir(sample_repo):
    hub = ToolHub(sample_repo)
    result = hub.run("list_files", {"subdir": "plan_agent"})
    assert "plan_agent/core.py" in result
    assert "tests/test_core.py" not in result


def test_list_files_on_missing_dir(sample_repo):
    hub = ToolHub(sample_repo)
    with pytest.raises(ToolError):
        hub.run("list_files", {"subdir": "no_such_dir"})

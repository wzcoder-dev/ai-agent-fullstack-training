"""可重现示例：错误的 add 函数和不可由 Agent 修改的测试。"""
from pathlib import Path

BROKEN = 'def add(a, b):\n    """返回两数之和。"""\n    return a - b\n'
FIXED = BROKEN.replace('return a - b', 'return a + b')
REPORT = '# 代码理解报告\n\ncalc.py 的 add(a, b) 应返回两数之和，目前却使用减法。\n测试 tests/test_calc.py 断言 add(2, 3) == 5，因此当前实现会失败。\n'


def init_workspace(root: Path):
    if root.exists() and any(root.iterdir()):
        raise ValueError('示例目录非空，请选择一个新目录')
    (root / 'tests').mkdir(parents=True, exist_ok=True)
    (root / 'calc.py').write_text(BROKEN)
    (root / 'tests/test_calc.py').write_text('from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n')


def script(kind):
    actions = [{'tool': 'read_file', 'args': {'path': 'calc.py'}},
               {'tool': 'read_file', 'args': {'path': 'tests/test_calc.py'}}]
    if kind == 'repair':
        actions += [{'tool': 'run_test', 'args': {'command': 'pytest -q'}},
                    {'tool': 'write_file', 'args': {'path': 'calc.py', 'content': FIXED}},
                    {'tool': 'run_test', 'args': {'command': 'pytest -q'}}]
    else:
        actions += [{'tool': 'write_file', 'args': {'path': 'artifacts/report.md', 'content': REPORT}}]
    return actions + [{'tool': 'final_answer', 'args': {'answer': '已完成，请核对持久化证据。'}}]

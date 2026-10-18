import pytest
from guarded_harness.demo import FIXED, REPORT
from guarded_harness.gate import Pause
from guarded_harness.models import ScriptedModel
from conftest import call, ready


def approved_write(h, path, content, id):
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path': path, 'content': content}, id)
    h.approve(id)


def test_h1_final_answer_without_deliverable_rejected(h):
    result = call(h, 'final_answer', {'answer': '我完成了！'})
    assert not result['ok']
    assert h.store.data['status'] == 'RUNNING'
    assert h.store.data['final_answer'] is None


def test_h1_framework_cannot_treat_rejected_final_as_success(h):
    h.store.data['max_steps'] = 2
    state = h.run(ScriptedModel([]), 'claim completion')
    assert state['status'] == 'STOPPED'
    assert state['final_answer'] is None
    assert state['stop_reason'] == 'MAX_STEPS'


def test_h2_source_change_invalidates_test_and_requires_rerun(h):
    approved_write(h, 'calc.py', FIXED, 'fix')
    assert call(h, 'run_test', {'command': 'pytest -q'}, 'test1')['returncode'] == 0
    assert h.completion()[0]
    approved_write(h, 'calc.py', FIXED + '\n# 后续修改\n', 'edit')
    assert not h.completion()[0]
    assert call(h, 'run_test', {'command': 'pytest -q'}, 'test2')['returncode'] == 0
    assert h.completion()[0]


def test_failed_tests_cannot_complete(h):
    approved_write(h, 'calc.py', 'def add(a, b):\n    return 0\n', 'wrong')
    assert call(h, 'run_test', {'command': 'pytest -q'}, 'test')['returncode'] != 0
    assert not h.completion()[0]


def test_understanding_requires_read_and_actual_report(h):
    h.store.data['kind'] = 'understand'
    assert not h.completion()[0]
    call(h, 'read_file', {'path': 'calc.py'}, 'read')
    assert not h.completion()[0]
    approved_write(h, 'artifacts/report.md', REPORT, 'report')
    assert h.completion()[0]
    # 修改报告后内容哈希不符，即使保留旧 evidence 也不能验收。
    (h.sandbox.root / 'artifacts/report.md').write_text('tampered')
    assert not h.completion()[0]


def test_changed_acceptance_suite_is_not_a_valid_repair(h):
    approved_write(h, 'calc.py', FIXED, 'fix')
    (h.sandbox.root / 'tests/test_calc.py').write_text('def test_nothing():\n    pass\n')
    assert h.completion() == (False, 'ACCEPTANCE_TESTS_CHANGED')

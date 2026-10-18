import pytest
from guarded_harness.gate import Pause
from guarded_harness.demo import FIXED
from conftest import call, records, ready


@pytest.mark.parametrize('tool,args', [('read_file', {'path': 3}), ('read_file', {}),
    ('read_file', {'path': 'calc.py', 'extra': True}), ('unknown', {}), ('read_file', 'calc.py')])
def test_g1_invalid_never_enters_handler(h, tool, args, monkeypatch):
    def forbidden(*a):
        pytest.fail('拒绝的调用不应进入 handler')
    monkeypatch.setattr(h, 'handle', forbidden)
    assert call(h, tool, args)['rule'] == 'INVALID_ARGUMENTS'
    assert records(h)[-1]['decision'] == 'DENY'


def test_g2_third_identical_call_blocked(h):
    for i in range(2):
        assert 'text' in call(h, 'read_file', {'path': 'calc.py'}, str(i))
    assert call(h, 'read_file', {'path': 'calc.py'}, 'third')['rule'] == 'REPEATED_ACTION'
    assert len(h.store.data['ledger']) == 2


def test_g3_write_pauses_without_mutation(h):
    before = (h.sandbox.root / 'calc.py').read_text()
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path': 'calc.py', 'content': FIXED})
    assert h.store.data['status'] == 'WAITING_APPROVAL'
    assert (h.sandbox.root / 'calc.py').read_text() == before
    assert all(c['tool'] != 'write_file' for c in h.store.data['ledger'].values())


def test_g4_approved_call_only_executes_once(h):
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path': 'calc.py', 'content': FIXED})
    h.approve('x', actor='test-human')
    assert (h.sandbox.root / 'calc.py').read_text() == FIXED
    assert call(h, 'write_file', {'path': 'calc.py', 'content': FIXED})['rule'] == 'DUPLICATE_CALL_ID'
    assert len([r for r in records(h) if r['rule'] == 'EXECUTION' and r['decision'] == 'DONE' and r['tool'] == 'write_file']) == 1
    assert h.store.data['calls'] == 2


def test_approval_is_bound_to_exact_arguments(h):
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path': 'calc.py', 'content': FIXED})
    s = h.store.data
    s['approvals']['x'] = {'signature': 'forged', 'revision': h.sandbox.revision()}
    s['status'] = 'RUNNING'
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path': 'calc.py', 'content': FIXED})
    assert s['status'] == 'WAITING_APPROVAL'


def test_understand_cannot_change_source(h):
    h.store.data['kind'] = 'understand'
    assert call(h, 'write_file', {'path': 'calc.py', 'content': FIXED})['rule'] == 'TASK_PERMISSION'


def test_cannot_modify_acceptance_tests(h):
    assert call(h, 'write_file', {'path': 'tests/test_calc.py', 'content': ''})['rule'] == 'TEST_WRITE_DENIED'


def test_human_can_deny_approval(h):
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path': 'calc.py', 'content': FIXED})
    h.approve('x', allow=False)
    assert h.store.data['stop_reason'] == 'APPROVAL_DENIED'


@pytest.mark.parametrize('path', ['./tests/conftest.py', './tests/test_calc.py', 'conftest.py'])
def test_normalized_test_path_cannot_bypass_permission(h, path):
    assert call(h, 'write_file', {'path': path, 'content': ''})['rule'] == 'TEST_WRITE_DENIED'

import json
import pytest
from guarded_harness.harness import Harness
from guarded_harness.demo import FIXED
from guarded_harness.gate import Pause
from conftest import call, ready


def reopen(h):
    return Harness(h.sandbox.root, h.checkpoint.directory, resume=True)


def test_c1_checkpoint_contains_business_state(h):
    call(h, 'read_file', {'path': 'calc.py'})
    data = json.loads(h.checkpoint.path.read_text())
    for key in ['run_id', 'steps', 'plan', 'evidence', 'calls', 'call_budget', 'approvals', 'ledger', 'pending']:
        assert key in data
    assert data['ledger']['x']['status'] == 'DONE'


def test_c2_unknown_write_result_never_replayed(h):
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path': 'calc.py', 'content': FIXED})
    class Crash(BaseException):
        pass
    def crash(tool):
        if tool == 'write_file':
            raise Crash()
    h.after_effect = crash
    with pytest.raises(Crash):
        h.approve('x')
    assert (h.sandbox.root / 'calc.py').read_text() == FIXED
    restored = reopen(h)
    assert restored.store.data['status'] == 'UNKNOWN_RESULT'
    assert restored.store.data['ledger']['x']['status'] == 'UNKNOWN_RESULT'
    with pytest.raises(Pause):
        call(restored, 'write_file', {'path': 'calc.py', 'content': 'bad'}, 'new')
    assert (h.sandbox.root / 'calc.py').read_text() == FIXED


def test_c3_restore_progress_counters_and_no_duplicate_write(h):
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path': 'calc.py', 'content': FIXED})
    h.approve('x')
    h.store.transition('USER_INTERRUPT', 'INTERRUPTED', 'USER_INTERRUPT')
    h.store.data['steps'] = 7
    h.checkpoint.save()
    restored = reopen(h)
    assert restored.store.data['steps'] == 7
    assert restored.store.data['calls'] == 2
    assert call(restored, 'write_file', {'path': 'calc.py', 'content': FIXED})['rule'] == 'DUPLICATE_CALL_ID'


def test_c4_changed_workspace_invalidates_old_approval_and_evidence(h):
    call(h, 'read_file', {'path': 'calc.py'})
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path': 'calc.py', 'content': FIXED}, 'write')
    (h.sandbox.root / 'calc.py').write_text('external change')
    restored = reopen(h)
    assert restored.store.data['status'] == 'NEEDS_REVIEW'
    assert restored.store.data['stop_reason'] == 'WORKSPACE_CHANGED'
    assert restored.store.data['evidence'] == []
    assert restored.store.data['approvals'] == {}
    assert restored.store.data['pending'] is None


def test_approval_checks_workspace_without_restarting(h):
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path': 'calc.py', 'content': FIXED})
    (h.sandbox.root / 'calc.py').write_text('changed')
    h.approve('x')
    assert h.store.data['status'] == 'NEEDS_REVIEW'
    assert (h.sandbox.root / 'calc.py').read_text() == 'changed'


def test_checkpoint_is_valid_json_after_each_safe_call(h):
    for i, path in enumerate(['calc.py', 'tests/test_calc.py']):
        call(h, 'read_file', {'path': path}, str(i))
        assert json.loads(h.checkpoint.path.read_text())['ledger'][str(i)]['status'] == 'DONE'

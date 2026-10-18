import json
import pytest
from guarded_harness.audit import Audit
from guarded_harness.gate import Pause
from guarded_harness.demo import FIXED
from conftest import call, records, ready


def test_a1_denied_call_has_decision_chain(h):
    call(h, 'read_file', {'path': '../bad'})
    r = records(h)[-1]
    assert r['rule'] == 'PATH_ESCAPE'
    assert r['decision'] == 'DENY'
    assert r['actor'] == 'program'
    assert r['tool_call_id'] == 'x'
    assert r['run_id'] == h.store.data['run_id']


def test_a2_trace_correlates_approval_execution_and_checkpoint(h):
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path': 'calc.py', 'content': FIXED})
    h.approve('x', actor='learner')
    rows = records(h)
    assert any(r['actor'] == 'learner' and r['decision'] == 'ALLOW' for r in rows)
    assert any(r['rule'] == 'EXECUTION' and r['decision'] == 'DONE' for r in rows)
    assert all(r['run_id'] == h.store.data['run_id'] for r in rows)
    assert json.loads(h.checkpoint.path.read_text())['ledger']['x']['status'] == 'DONE'
    Audit(h.audit.path, h.store.data['run_id'])


def test_tampered_audit_chain_detected(h):
    lines = h.audit.path.read_text().splitlines()
    r = json.loads(lines[0]); r['decision'] = 'forged'
    h.audit.path.write_text(json.dumps(r) + '\n')
    with pytest.raises(ValueError, match='审计链'):
        Audit(h.audit.path, h.store.data['run_id'])

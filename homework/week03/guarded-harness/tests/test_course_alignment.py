"""六项课程借鉴的行为回归：验证机制，不复制实现判断。"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import pytest
from smolagents.models import ChatMessage, MessageRole
from smolagents.monitoring import TokenUsage
from guarded_harness.checkpoint import CheckpointError
from guarded_harness.context_budget import render_context, size, recovery_context
from guarded_harness.demo import FIXED, REPORT
from guarded_harness.gate import Pause
from guarded_harness.harness import Harness
from guarded_harness.models import MeteredModel, ScriptedModel
from guarded_harness.state_store import digest
from conftest import call, ready
from test_completion import approved_write


@pytest.mark.parametrize('returncode,reason', [(1, None), (-9, 'TIMEOUT'), (-9, 'CANCELLED'), (None, 'UNKNOWN_RESULT')])
def test_r1_latest_failed_or_unknown_attempt_blocks_old_pass(h, monkeypatch, returncode, reason):
    approved_write(h, 'calc.py', FIXED, 'fix')
    assert call(h, 'run_test', {'command': 'pytest -q'}, 'pass')['returncode'] == 0
    revision = h.sandbox.revision()
    monkeypatch.setattr(h.sandbox, 'run_test', lambda **a: dict(returncode=returncode, reason=reason, output='injected failure', truncated=False))
    call(h, 'run_test', {'command': 'pytest -q'}, 'later')
    assert h.sandbox.revision() == revision  # 无源码修改，专门验证同一版本的最新结果。
    assert any(e['kind'] == 'test' and e['returncode'] == 0 for e in h.store.data['evidence'])
    assert not h.completion()[0]
    assert 'LATEST_TEST_NOT_PASSED' in {x['code'] for x in h.completion_result().missing}
    assert h.plan.get('verify')['status'] == 'failed'


def test_r1_handler_exception_is_new_unknown_evidence(h, monkeypatch):
    approved_write(h, 'calc.py', FIXED, 'fix')
    call(h, 'run_test', {'command': 'pytest -q'}, 'pass')
    def fail(**kwargs):
        raise OSError('测试执行器异常')
    monkeypatch.setattr(h.sandbox, 'run_test', fail)
    call(h, 'run_test', {'command': 'pytest -q'}, 'unknown')
    assert h.store.data['evidence'][-1]['reason'] == 'UNKNOWN_RESULT'
    assert not h.completion()[0]


def test_r1_latest_success_can_recover_failed_verification(h, monkeypatch):
    approved_write(h, 'calc.py', FIXED, 'fix')
    with monkeypatch.context() as patch:
        patch.setattr(h.sandbox, 'run_test', lambda **a: dict(returncode=1, reason=None, output='failed', truncated=False))
        call(h, 'run_test', {'command': 'pytest -q'}, 'failed')
    assert not h.completion()[0]
    assert call(h, 'run_test', {'command': 'pytest -q'}, 'new-pass')['returncode'] == 0
    assert h.completion()[0]


def test_r2_completion_returns_all_missing_items_to_model(h):
    result = call(h, 'final_answer', {'answer': '完成'}, 'final')
    codes = {item['code'] for item in result['missing']}
    assert {'MISSING_SOURCE_DIFF', 'MISSING_CURRENT_TEST', 'PLAN_INCOMPLETE'} <= codes
    assert all(item['message'] for item in result['missing'])
    assert h.store.data['status'] == 'RUNNING'


def test_r2_wrong_report_path_does_not_satisfy_contract(h):
    h.store.data['kind'] = 'understand'
    approved_write(h, 'artifacts/other.md', REPORT, 'other')
    assert not h.completion()[0]
    assert any(x['code'] == 'MISSING_REPORT' and 'artifacts/report.md' in x['message'] for x in h.completion_result().missing)
    approved_write(h, 'artifacts/report.md', REPORT, 'right')
    assert h.completion()[0]


def test_r2_report_must_contain_text(h):
    h.store.data['kind'] = 'understand'
    approved_write(h, 'artifacts/report.md', '  \n', 'empty')
    assert not h.completion()[0]
    assert 'EMPTY_REPORT' in {x['code'] for x in h.completion_result().missing}


def test_r3_illegal_terminal_transition_rejected(h):
    h.store.transition('STOP', 'STOPPED', 'test')
    before = copy.deepcopy(h.store.data)
    with pytest.raises(ValueError, match='非法状态迁移'):
        h.store.transition('RESUME', 'RUNNING')
    assert h.store.data == before


def test_r3_transition_sequence_survives_new_process(h):
    ready(h)
    h.store.transition('USER_INTERRUPT', 'INTERRUPTED', 'USER_INTERRUPT')
    h.checkpoint.save()
    before = len(h.store.data['transitions'])
    restored = Harness(h.sandbox.root, h.checkpoint.directory, resume=True)
    history = restored.store.data['transitions']
    assert len(history) == before + 1
    assert [t['seq'] for t in history] == list(range(1, len(history)+1))
    assert history[-1]['from_state'] == 'INTERRUPTED' and history[-1]['to_state'] == 'RUNNING'


def test_r4_write_requires_completed_read_step(h):
    result = call(h, 'write_file', {'path': 'calc.py', 'content': FIXED}, 'premature')
    assert result['rule'] == 'PLAN_DEPENDENCY'
    assert h.store.data['pending'] is None
    assert h.plan.get('change')['attempts'] == 0


def test_r4_steps_link_real_evidence_and_invalidate_on_change(h):
    approved_write(h, 'calc.py', FIXED, 'fix')
    call(h, 'run_test', {'command': 'pytest -q'}, 'verify')
    for id in ['inspect', 'change', 'verify']:
        step = h.plan.get(id)
        assert step['status'] == 'completed'
        assert step['attempts'] >= 1
        assert step['evidence_ids']
        assert set(step['evidence_ids']) <= {e['id'] for e in h.store.data['evidence']}
    approved_write(h, 'calc.py', FIXED + '\n# edit\n', 'edit')
    assert h.plan.get('verify')['status'] == 'pending'
    assert h.plan.get('finalize')['status'] == 'pending'
    assert not h.completion()[0]


def test_r4_baseline_test_cannot_replace_source_read(h):
    call(h, 'run_test', {'command': 'pytest -q'}, 'baseline')
    assert h.plan.get('inspect')['status'] != 'completed'
    assert call(h, 'write_file', {'path': 'calc.py', 'content': FIXED}, 'early')['rule'] == 'PLAN_DEPENDENCY'


def test_r5_context_compaction_is_deterministic_and_keeps_recent_pair():
    messages = [ChatMessage(role=MessageRole.SYSTEM, content='系统约束不能裁剪'), ChatMessage(role=MessageRole.USER, content='任务不能裁剪')]
    for i in range(5):
        messages += [ChatMessage(role=MessageRole.ASSISTANT, content=f'thought-{i}'),
                     ChatMessage(role=MessageRole.TOOL_CALL, content=f'read_file-{i}'),
                     ChatMessage(role=MessageRole.TOOL_RESPONSE, content=f'result-{i}:' + 'x'*1800)]
    before = copy.deepcopy(messages)
    rendered, stats = render_context(messages, max_chars=3200, keep_recent=1)
    again, second = render_context(messages, max_chars=3200, keep_recent=1)
    assert stats == second and rendered == again
    assert stats['applied'] and size(rendered) <= 3200
    assert rendered[-2].content == 'read_file-4' and rendered[-1].content.startswith('result-4:')
    assert messages == before
    assert rendered[0].content == '系统约束不能裁剪'
    assert rendered[1].content == '任务不能裁剪'


def test_r5_pinned_context_cannot_be_silently_truncated():
    with pytest.raises(ValueError, match='CONTEXT_BUDGET'):
        render_context([ChatMessage(role=MessageRole.SYSTEM, content='x'*5000)], max_chars=100)


def test_r5_oversized_task_stops_before_model_request(h):
    h.store.data['context_max_chars'] = 100
    state = h.run(ScriptedModel([]), '任务')
    assert state['stop_reason'] == 'CONTEXT_BUDGET'
    assert state['steps'] == 0


class UsageModel(ScriptedModel):
    def generate(self, messages, **kwargs):
        message = super().generate(messages, **kwargs)
        message.token_usage = TokenUsage(input_tokens=7, output_tokens=3)
        return message


def test_r5_tokens_persist_and_exhaustion_stops_before_tool(h):
    h.store.data['max_total_tokens'] = 15
    MeteredModel(UsageModel([]), h).generate([ChatMessage(role='user', content='任务')])
    assert h.store.data['tokens_used'] == 10
    restored = Harness(h.sandbox.root, h.checkpoint.directory, resume=True)
    assert restored.store.data['tokens_used'] == 10
    with pytest.raises(Pause):
        MeteredModel(UsageModel([]), restored).generate([ChatMessage(role='user', content='继续')])
    assert restored.store.data['tokens_used'] == 20
    assert restored.store.data['stop_reason'] == 'TOKEN_BUDGET'
    assert restored.store.data['ledger'] == {}


def test_r5_active_duration_persists_without_charging_pause(h, tmp_path):
    now = [0.0]
    run = tmp_path / 'clock-run'
    first = Harness(h.sandbox.root, run, max_duration_seconds=5, clock=lambda: now[0])
    now[0] = 2
    first.checkpoint.save()
    now[0] = 100  # 两次命令之间的暂停，不计入有效运行耗时。
    second = Harness(h.sandbox.root, run, resume=True, clock=lambda: now[0])
    second.sync_elapsed()
    assert second.store.data['elapsed_seconds'] == 2
    now[0] = 104
    with pytest.raises(Pause):
        second.enforce_budget()
    assert second.store.data['stop_reason'] == 'DURATION_BUDGET'
    assert second.store.data['elapsed_seconds'] == 6


def test_r5_recovery_context_does_not_copy_all_historical_outputs(h):
    h.store.data['history'] = [{'id': str(i), 'tool': 'read_file', 'result': {'text': 'x'*10000}} for i in range(20)]
    payload = recovery_context(h.store.data)
    assert len(payload['recent_history']) == 2
    assert 'text' not in payload['old_steps'][0]
    assert len(json.dumps(payload)) < len(json.dumps(h.store.data['history'])) / 2


def files_snapshot(root):
    return {str(f.relative_to(root)): (f.read_bytes(), f.stat().st_mtime_ns) for f in root.rglob('*') if f.is_file()}


def test_r6_inspect_and_status_are_read_only_commands(h):
    ready(h)
    before = files_snapshot(h.checkpoint.directory)
    for action in ['inspect', 'status']:
        p = subprocess.run([sys.executable, '-m', 'guarded_harness.cli', action, '--workspace', str(h.sandbox.root),
                            '--run-dir', str(h.checkpoint.directory)], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        data = json.loads(p.stdout)
        assert (data['can_resume'] if action == 'inspect' else data['inspection']['can_resume'])
    assert files_snapshot(h.checkpoint.directory) == before


def test_r6_missing_checkpoint_inspect_does_not_create_directory(h, tmp_path):
    directory = tmp_path / 'absent'
    p = subprocess.run([sys.executable, '-m', 'guarded_harness.cli', 'inspect', '--workspace', str(h.sandbox.root),
                        '--run-dir', str(directory)], capture_output=True, text=True)
    assert not json.loads(p.stdout)['valid']
    assert not directory.exists()


def test_r6_checksum_tampering_rejected_without_overwrite(h):
    payload = json.loads(h.checkpoint.path.read_text())
    payload['steps'] = 900
    h.checkpoint.path.write_text(json.dumps(payload))
    before = h.checkpoint.path.read_bytes()
    report = h.checkpoint.inspect()
    assert not report['valid'] and 'CHECKPOINT_CHECKSUM' in report['notes'][0]
    with pytest.raises(CheckpointError):
        Harness(h.sandbox.root, h.checkpoint.directory, resume=True)
    assert h.checkpoint.path.read_bytes() == before


@pytest.mark.parametrize('field,value', [('tokens_used', -1), ('plan', []), ('ledger', []), ('transitions', []), ('elapsed_seconds', 'bad')])
def test_r6_schema_rejected_even_with_matching_checksum(h, field, value):
    payload = json.loads(h.checkpoint.path.read_text())
    payload.pop('checksum')
    payload[field] = value
    payload['checksum'] = digest(payload)
    h.checkpoint.path.write_text(json.dumps(payload))
    report = h.checkpoint.inspect()
    assert not report['valid']
    assert 'CHECKPOINT_SCHEMA' in report['notes'][0]


def test_r6_inspection_names_changed_files_without_mutation(h):
    ready(h)
    (h.sandbox.root / 'calc.py').write_text('external')
    before = files_snapshot(h.checkpoint.directory)
    report = h.checkpoint.inspect()
    assert report['code'] == 'WORKSPACE_CHANGED'
    assert report['changed_files'][0]['path'] == 'calc.py'
    assert not report['can_resume']
    assert files_snapshot(h.checkpoint.directory) == before
    restored = Harness(h.sandbox.root, h.checkpoint.directory, resume=True)
    assert restored.store.data['status'] == 'NEEDS_REVIEW'
    assert restored.store.data['resume_inspection']['changed_files'][0]['path'] == 'calc.py'


def test_r6_pending_approval_report_does_not_start_model(h):
    ready(h)
    with pytest.raises(Pause):
        call(h, 'write_file', {'path':'calc.py', 'content':FIXED}, 'write')
    report = h.checkpoint.inspect()
    assert report['can_approve'] and not report['can_resume']
    assert report['code'] == 'WAITING_APPROVAL'


def test_r6_completed_run_inspection_refuses_resume(h):
    ready(h)
    h.store.transition('TEST_END', 'COMPLETED', 'VERIFIED')
    h.checkpoint.save()
    report = h.checkpoint.inspect()
    assert report['code'] == 'RUN_TERMINAL' and not report['can_resume']


def test_r6_old_checkpoint_version_is_read_only(h):
    payload = json.loads(h.checkpoint.path.read_text())
    payload['version'] = 1
    h.checkpoint.path.write_text(json.dumps(payload))
    before = h.checkpoint.path.read_bytes()
    assert 'CHECKPOINT_VERSION' in h.checkpoint.inspect()['notes'][0]
    assert h.checkpoint.path.read_bytes() == before


def test_r4_completed_inspection_survives_unrelated_read_failure(h):
    ready(h)
    assert call(h, 'read_file', {'path':'missing.py'}, 'missing')['rule'] == 'HANDLER_ERROR'
    assert h.plan.get('inspect')['status'] == 'completed'
    approved_write(h, 'calc.py', FIXED, 'fix')
    assert (h.sandbox.root/'calc.py').read_text() == FIXED


def test_r4_normalized_test_path_cannot_satisfy_source_read(h):
    call(h, 'read_file', {'path':'./tests/test_calc.py'}, 'test-only')
    assert h.plan.get('inspect')['status'] != 'completed'
    assert call(h, 'write_file', {'path':'calc.py','content':FIXED}, 'early')['rule'] == 'PLAN_DEPENDENCY'

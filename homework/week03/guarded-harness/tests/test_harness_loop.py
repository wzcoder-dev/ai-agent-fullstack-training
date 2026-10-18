import pytest
from smolagents import ToolCallingAgent
from smolagents.models import ChatMessage
from guarded_harness.demo import script
from guarded_harness.harness import Harness, GuardedAgent
from guarded_harness.models import ScriptedModel


@pytest.mark.parametrize('kind', ['repair', 'understand'])
def test_real_smolagents_loop_with_fake_model_and_process_recovery(h, kind):
    h.store.data['kind'] = kind
    h.checkpoint.save()
    assert isinstance(GuardedAgent(h, ScriptedModel([])), ToolCallingAgent)
    state = h.run(ScriptedModel(script(kind)), '完成任务')
    assert state['status'] == 'WAITING_APPROVAL'
    restored = Harness(h.sandbox.root, h.checkpoint.directory, resume=True)
    restored.approve(state['pending']['id'])
    state = restored.run(ScriptedModel(script(kind), cursor=state['steps']), '继续任务')
    assert state['status'] == 'COMPLETED'
    assert state['stop_reason'] == 'VERIFIED'


def test_h3_max_steps_stops_without_fake_success(h):
    h.store.data['max_steps'] = 2
    actions = [{'tool': 'read_file', 'args': {'path': p}} for p in ['calc.py', 'tests/test_calc.py']]
    state = h.run(ScriptedModel(actions), 'read')
    assert state['status'] == 'STOPPED'
    assert state['stop_reason'] == 'MAX_STEPS'
    assert state['steps'] == 2
    assert state['final_answer'] is None


def test_h3_call_budget_stops(h):
    h.store.data['call_budget'] = 1
    state = h.run(ScriptedModel(script('repair')), 'repair')
    assert state['stop_reason'] == 'CALL_BUDGET'
    assert state['calls'] == 1


def test_model_exception_stops_and_persists(h):
    class BrokenModel(ScriptedModel):
        def generate(self, *args, **kwargs):
            raise RuntimeError('provider down')
    assert h.run(BrokenModel([]), 'task')['stop_reason'] == 'MODEL_OR_RUNTIME_ERROR'
    assert h.checkpoint.path.exists()


def test_zero_call_batch_stops(h):
    class EmptyModel(ScriptedModel):
        def generate(self, *args, **kwargs):
            return ChatMessage(role='assistant', tool_calls=[])
        def parse_tool_calls(self, message):
            return message
    assert h.run(EmptyModel([]), 'task')['stop_reason'] == 'INVALID_CALL_BATCH'


def test_cli_refuses_concurrent_writer(h):
    import fcntl
    import subprocess
    import sys
    with (h.checkpoint.directory / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        p = subprocess.run([sys.executable, '-m', 'guarded_harness.cli', 'status',
                            '--workspace', str(h.sandbox.root), '--run-dir', str(h.checkpoint.directory)],
                           capture_output=True, text=True)
    assert p.returncode == 2
    assert '另一个进程' in p.stderr

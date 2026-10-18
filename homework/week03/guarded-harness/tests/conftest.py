import json
import pytest
from guarded_harness.demo import init_workspace
from guarded_harness.harness import Harness


@pytest.fixture
def h(tmp_path):
    root = tmp_path / 'repo'
    init_workspace(root)
    return Harness(root, tmp_path / 'run')


def call(h, tool, args, id='x'):
    return h.gate.dispatch(id, tool, args)


def records(h):
    return [json.loads(line) for line in h.audit.path.read_text().splitlines()]


def ready(h):
    """执行真实源码读取，满足修改/报告步骤的依赖；不伪造计划完成状态。"""
    if h.plan.get('inspect')['status'] != 'completed':
        call(h, 'read_file', {'path': 'calc.py'}, 'prepare-read')

"""业务事实、状态迁移与预算是持久化对象；模型只能提出动作。"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
from .plan_store import new_plan

STATES = {'RUNNING', 'WAITING_APPROVAL', 'INTERRUPTED', 'UNKNOWN_RESULT', 'NEEDS_REVIEW', 'STOPPED', 'COMPLETED'}
TERMINAL = {'UNKNOWN_RESULT', 'NEEDS_REVIEW', 'STOPPED', 'COMPLETED'}
ALLOWED = {
    'RUNNING': STATES,
    'WAITING_APPROVAL': {'WAITING_APPROVAL', 'RUNNING', 'STOPPED', 'NEEDS_REVIEW', 'UNKNOWN_RESULT'},
    'INTERRUPTED': {'INTERRUPTED', 'RUNNING', 'STOPPED', 'NEEDS_REVIEW', 'UNKNOWN_RESULT'},
    **{s: {s} for s in TERMINAL},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_json(path: Path, value: dict):
    """先 fsync 临时文件再原子替换，并 fsync 父目录，避免半份存档。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class StateStore:
    def __init__(self, root, kind='repair', max_steps=16, call_budget=24,
                 max_total_tokens=50000, max_duration_seconds=300, context_max_chars=24000, keep_recent=2):
        if min(max_steps, call_budget, max_total_tokens, max_duration_seconds, context_max_chars) <= 0 or keep_recent < 0:
            raise ValueError('预算必须为正数；keep_recent 不得为负')
        self.data = dict(version=2, run_id=uuid4().hex, root=str(root.resolve()), kind=kind,
                         status='RUNNING', stop_reason=None, steps=0, calls=0,
                         max_steps=max_steps, call_budget=call_budget, tokens_used=0,
                         max_total_tokens=max_total_tokens, elapsed_seconds=0.0,
                         max_duration_seconds=max_duration_seconds, context_max_chars=context_max_chars,
                         keep_recent=keep_recent, context_stats=[], plan=new_plan(kind),
                         evidence=[], evidence_sequence=0, approvals={}, ledger={}, pending=None,
                         last_signature=None, repeat_count=0, history=[], expected_snapshot=None,
                         initial_sources={}, final_answer=None, transitions=[], resume_inspection=None)
        self.transition('START', 'RUNNING')

    def transition(self, event, to, reason=None):
        """统一入口：检查合法迁移，序号跨进程延续，审计另保留外部事件。"""
        state = self.data
        old = state['status']
        if to not in ALLOWED.get(old, set()):
            raise ValueError(f'非法状态迁移：{old} -> {to} ({event})')
        state['transitions'].append(dict(seq=len(state['transitions']) + 1, event=event,
                                         from_state=old, to_state=to, reason=reason))
        state.update(status=to, stop_reason=reason)
        return state['transitions'][-1]

    def add_evidence(self, **fields):
        self.data['evidence_sequence'] += 1
        item = dict(id=f'ev-{self.data["evidence_sequence"]}', **fields)
        self.data['evidence'].append(item)
        return item

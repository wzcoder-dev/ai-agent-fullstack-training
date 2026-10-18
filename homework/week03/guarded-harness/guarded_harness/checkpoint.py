"""版本化 Checkpoint：原子存档 + 校验和 + 先检查再恢复。"""
from __future__ import annotations
import json
import math
from pathlib import Path
from .state_store import atomic_json, digest, STATES, TERMINAL, ALLOWED
from .plan_store import PlanStore


class CheckpointError(ValueError):
    def __init__(self, report):
        super().__init__(report['code'] + ': ' + '; '.join(report['notes']))
        self.report = report


def validate(data):
    """不信任“能解析成 JSON”就等于结构正确；特别检查恢复所依赖的嵌套字段。"""
    if not isinstance(data, dict) or data.get('version') != 2:
        raise ValueError('CHECKPOINT_VERSION：只接受 v2；旧证据保留只读，不自动升级续跑')
    checksum = data.get('checksum')
    payload = {k:v for k,v in data.items() if k != 'checksum'}
    if not isinstance(checksum, str) or digest(payload) != checksum:
        raise ValueError('CHECKPOINT_CHECKSUM：存档校验和不一致')
    for key in ['run_id', 'root', 'kind', 'status']:
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError('CHECKPOINT_SCHEMA：字符串字段错误 ' + key)
    if data['status'] not in STATES or data['kind'] not in {'repair', 'understand'}:
        raise ValueError('CHECKPOINT_SCHEMA：状态或任务类型无效')
    for key in ['steps', 'calls', 'max_steps', 'call_budget', 'tokens_used', 'max_total_tokens',
                'context_max_chars', 'keep_recent', 'evidence_sequence', 'repeat_count']:
        if type(data.get(key)) is not int or data[key] < 0:
            raise ValueError('CHECKPOINT_SCHEMA：计数或预算无效 ' + key)
    for key in ['max_steps', 'call_budget', 'max_total_tokens', 'context_max_chars']:
        if data[key] <= 0:
            raise ValueError('CHECKPOINT_SCHEMA：预算必须为正 ' + key)
    for key in ['elapsed_seconds', 'max_duration_seconds']:
        if type(data.get(key)) not in (int, float) or not math.isfinite(data[key]) or data[key] < 0:
            raise ValueError('CHECKPOINT_SCHEMA：耗时字段无效 ' + key)
    if data['max_duration_seconds'] <= 0:
        raise ValueError('CHECKPOINT_SCHEMA：总时限必须为正')
    for key in ['evidence', 'history', 'transitions', 'context_stats']:
        if not isinstance(data.get(key), list):
            raise ValueError('CHECKPOINT_SCHEMA：列表字段错误 ' + key)
    for key in ['ledger', 'approvals', 'initial_sources', 'expected_snapshot', 'plan']:
        if not isinstance(data.get(key), dict):
            raise ValueError('CHECKPOINT_SCHEMA：对象字段错误 ' + key)
    plan = data['plan']
    if plan.get('kind') != data['kind'] or not isinstance(plan.get('steps'), list):
        raise ValueError('CHECKPOINT_SCHEMA：计划与任务不一致')
    ids = [s.get('id') for s in plan['steps'] if isinstance(s, dict)]
    required = {'inspect', 'change', 'verify', 'finalize'} if data['kind'] == 'repair' else {'inspect', 'report', 'finalize'}
    if len(ids) != len(required) or set(ids) != required:
        raise ValueError('CHECKPOINT_SCHEMA：计划步骤缺失或重复')
    evidence_ids = set()
    for e in data['evidence']:
        if not isinstance(e, dict) or not isinstance(e.get('id'), str) or e['id'] in evidence_ids or e.get('kind') not in {'read','write','test'} or not isinstance(e.get('call_id'), str):
            raise ValueError('CHECKPOINT_SCHEMA：证据无效')
        evidence_ids.add(e['id'])
    for step in plan['steps']:
        if (step.get('status') not in {'pending','in_progress','completed','failed','blocked'}
            or not isinstance(step.get('depends_on'), list) or not set(step['depends_on']) <= required
            or type(step.get('attempts')) is not int or step['attempts'] < 0
            or not isinstance(step.get('evidence_ids'), list) or not set(step['evidence_ids']) <= evidence_ids):
            raise ValueError('CHECKPOINT_SCHEMA：步骤结构或证据引用错误')
    for key, call in data['ledger'].items():
        if not isinstance(call, dict) or call.get('status') not in {'EXECUTING','DONE','UNKNOWN_RESULT'} or not isinstance(call.get('signature'), str):
            raise ValueError('CHECKPOINT_SCHEMA：调用账本错误')
    if data['pending'] is not None:
        p = data['pending']
        if not isinstance(p, dict) or any(not isinstance(p.get(k), str) for k in ['id','tool','signature','revision']) or not isinstance(p.get('args'), dict):
            raise ValueError('CHECKPOINT_SCHEMA：待审批记录错误')
    if data['status'] == 'WAITING_APPROVAL' and data['pending'] is None:
        raise ValueError('CHECKPOINT_SCHEMA：等待审批却没有请求')
    previous = None
    for i, t in enumerate(data['transitions'], 1):
        if not isinstance(t, dict) or t.get('seq') != i or t.get('to_state') not in ALLOWED.get(t.get('from_state'), set()) or (previous is not None and t['from_state'] != previous):
            raise ValueError('CHECKPOINT_SCHEMA：迁移历史不连续或非法')
        previous = t['to_state']
    if previous != data['status']:
        raise ValueError('CHECKPOINT_SCHEMA：最终状态与迁移历史不一致')
    return payload


class Checkpoint:
    def __init__(self, directory: Path, store, sandbox):
        self.directory, self.store, self.sandbox = directory, store, sandbox
        self.path = directory / 'checkpoint.json'
        self.before_save = None

    def save(self, refresh=True):
        if self.before_save:
            self.before_save()
        if refresh:
            self.store.data['expected_snapshot'] = self.sandbox.manifest()
        payload = self.store.data
        atomic_json(self.path, {**payload, 'checksum': digest(payload)})

    def inspect(self):
        """纯读取：不初始化模型，不追加审计，不改 Checkpoint、预算或工作区。"""
        report = dict(valid=False, can_resume=False, can_approve=False, code='CHECKPOINT_INVALID',
                      notes=[], changed_files=[], unresolved=[], state=None)
        try:
            data = validate(json.loads(self.path.read_text()))
            if data['root'] != str(self.sandbox.root):
                raise ValueError('WORKSPACE_MISMATCH：工作区路径与存档不同')
            if not self.sandbox.root.is_dir():
                raise ValueError('WORKSPACE_MISSING：工作区不存在')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            report['notes'] = [str(exc)]
            return report
        report.update(valid=True, state=data['status'], run_id=data['run_id'])
        actual, expected = self.sandbox.manifest(), data['expected_snapshot']
        report['changed_files'] = [dict(path=p, change='added' if p not in expected else 'removed' if p not in actual else 'modified',
                                       before=expected.get(p), after=actual.get(p))
                                   for p in sorted(set(actual) | set(expected)) if actual.get(p) != expected.get(p)]
        report['unresolved'] = [dict(tool_call_id=id, tool=c['tool'], state=c['status'],
                                    action='人工核对真实文件；不得自动重放')
                                for id,c in data['ledger'].items() if c['status'] in {'EXECUTING','UNKNOWN_RESULT'}]
        if report['unresolved']:
            report.update(code='UNKNOWN_RESULT', notes=['存在已启动但结果未闭合的调用。先核对副作用，禁止自动重放。'])
        elif data['status'] in TERMINAL:
            report.update(code='RUN_TERMINAL', notes=[f'运行已处于 {data["status"]}，不会重启模型；核对后另建新 run。'])
        elif report['changed_files']:
            report.update(code='WORKSPACE_CHANGED', notes=['工作区与快照不同，旧证据和审批必须失效；请核对列出的文件。'])
        elif data['status'] == 'WAITING_APPROVAL':
            report.update(code='WAITING_APPROVAL', can_approve=True, notes=['请求尚未执行。核对 Diff 后批准或拒绝，不启动模型。'])
        else:
            report.update(code='READY', can_resume=True, notes=['结构、校验和、工作区和调用账本检查通过，可以恢复。'])
        return report

    def load(self):
        report = self.inspect()
        if not report['valid']:
            raise CheckpointError(report)
        data = validate(json.loads(self.path.read_text()))
        self.store.data = data
        data['resume_inspection'] = report
        if report['code'] == 'UNKNOWN_RESULT':
            for call in data['ledger'].values():
                if call['status'] == 'EXECUTING':
                    call['status'] = 'UNKNOWN_RESULT'
            if data['status'] not in TERMINAL:
                self.store.transition('RECOVERY_UNCERTAIN', 'UNKNOWN_RESULT', 'UNKNOWN_RESULT')
            data.update(approvals={}, pending=None)
        elif report['code'] == 'WORKSPACE_CHANGED':
            self.store.transition('RECOVERY_WORKSPACE_CHANGED', 'NEEDS_REVIEW', 'WORKSPACE_CHANGED')
            data.update(evidence=[], approvals={}, pending=None)
            for step in data['plan']['steps']:
                step['evidence_ids'] = []
            PlanStore(data).block_all('工作区变化，证据失效')
        elif report['can_resume']:
            self.store.transition('RESUME', 'RUNNING')
        self.save(refresh=False)
        return data

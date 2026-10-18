"""借鉴课程 3-3/3-5 的 PlanStep：目标、依赖、状态和证据各有位置。"""
from __future__ import annotations
from pathlib import Path


def is_source_path(path):
    rel = Path(path).as_posix()
    return rel.endswith('.py') and not rel.startswith(('tests/', 'artifacts/')) and not Path(rel).name.startswith('test_') and Path(rel).name != 'conftest.py'


def new_plan(kind):
    specs = [('inspect', '已读取相关源码，理解任务现场', [], ['read'])]
    if kind == 'repair':
        specs += [('change', '源码已按审批请求修改', ['inspect'], ['write']),
                  ('verify', '当前版本最后一次测试通过', ['change'], ['test']),
                  ('finalize', '完成契约接受交付', ['verify'], [])]
    else:
        specs += [('report', '约定报告已真实写入', ['inspect'], ['write']),
                  ('finalize', '完成契约接受交付', ['report'], [])]
    return dict(version=1, kind=kind, steps=[dict(id=id, objective=goal, depends_on=deps,
                success_criteria=criteria, status='pending', attempts=0, evidence_ids=[], last_error=None)
                for id, goal, deps, criteria in specs])


class PlanStore:
    def __init__(self, state):
        self.state = state
        # 只允许尚未运行的测试 fixture 改任务类型；生产恢复由 Checkpoint 校验类型一致。
        if state['plan']['kind'] != state['kind']:
            if state['ledger'] or state['evidence']:
                raise ValueError('已有执行事实，不能改变计划类型')
            state['plan'] = new_plan(state['kind'])

    def get(self, id):
        return next(s for s in self.state['plan']['steps'] if s['id'] == id)

    def dependencies(self, id):
        return [dep for dep in self.get(id)['depends_on'] if self.get(dep)['status'] != 'completed']

    def for_tool(self, tool, args):
        if tool == 'read_file':
            return 'inspect'
        if tool == 'run_test':
            return 'verify' if self.state['kind'] == 'repair' and self.get('change')['status'] == 'completed' else 'inspect'
        if tool == 'write_file':
            return 'report' if self.state['kind'] == 'understand' else (
                'inspect' if Path(args['path']).as_posix().startswith('artifacts/') else 'change')
        return 'finalize'

    def begin(self, id):
        missing = self.dependencies(id)
        if missing:
            raise ValueError('步骤依赖未完成：' + ', '.join(missing))
        step = self.get(id)
        step['attempts'] += 1
        # 重读文件不抹掉已经建立的理解事实；重跑验证必须撤回此前“完成”。
        if id != 'inspect' or step['status'] != 'completed':
            step['status'] = 'in_progress'
        step['last_error'] = None

    def observe(self, id, evidence=None, success=True, error=None):
        step = self.get(id)
        if evidence and evidence['id'] not in step['evidence_ids']:
            step['evidence_ids'].append(evidence['id'])
        step.update(status='completed' if success else 'failed', last_error=error)

    def invalidate_verification(self):
        for step in self.state['plan']['steps']:
            if step['id'] in {'verify', 'finalize'}:
                step.update(status='pending', last_error='源码变化，需重新验证')

    def block_all(self, reason):
        for step in self.state['plan']['steps']:
            step.update(status='blocked', last_error=reason)

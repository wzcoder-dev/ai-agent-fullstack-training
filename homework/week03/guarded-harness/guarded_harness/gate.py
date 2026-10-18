"""所有调用（包括 final_answer）经过同一 Gate；handler 不承担审批规则。"""
from __future__ import annotations

from .state_store import digest
from .sandbox import BoundaryError
from .plan_store import is_source_path


class Pause(BaseException):
    """控制流信号：避免被框架的 except Exception 当成可重试工具错误。"""


SCHEMAS = {'read_file': {'path': str}, 'write_file': {'path': str, 'content': str},
           'run_test': {'command': str}, 'final_answer': {'answer': str}}


class Gate:
    def __init__(self, harness):
        self.h = harness

    def reject(self, call_id, rule, missing=None):
        self.h.audit.record(rule, 'DENY', call_id, tool=self.h.current_tool)
        result = {'ok': False, 'rule': rule, 'tool_call_id': call_id, 'missing': missing or []}
        self.h.store.data['history'].append({'id': call_id, 'result': result})
        self.h.checkpoint.save(refresh=False)
        return result

    def dispatch(self, call_id: str, tool: str, args):
        h, s = self.h, self.h.store.data
        h.current_tool = tool
        if s['status'] != 'RUNNING':
            raise Pause()
        if not isinstance(args, dict) or tool not in SCHEMAS or set(args) != set(SCHEMAS[tool]) or any(
            type(args[k]) is not typ for k, typ in SCHEMAS.get(tool, {}).items()
        ):
            return self.reject(call_id, 'INVALID_ARGUMENTS')
        h.enforce_budget()
        signature = digest([tool, args])
        old = s['ledger'].get(call_id)
        if old:
            return self.reject(call_id, 'DUPLICATE_CALL_ID')
        resumed = s['pending'] and s['pending']['id'] == call_id and call_id in s['approvals']
        if not resumed and s['calls'] >= s['call_budget']:
            h.stop('CALL_BUDGET')
        if not resumed:
            s['calls'] += 1
            s['repeat_count'] = s['repeat_count'] + 1 if signature == s['last_signature'] else 1
            s['last_signature'] = signature
        if s['repeat_count'] >= 3:
            return self.reject(call_id, 'REPEATED_ACTION')
        try:
            if tool in {'read_file', 'write_file'}:
                h.sandbox.path(args['path'], write=tool == 'write_file')
            if tool == 'run_test' and args['command'] != 'pytest -q':
                raise BoundaryError('COMMAND_DENIED')
        except BoundaryError as exc:
            return self.reject(call_id, str(exc))
        revision = h.sandbox.revision()
        # 外部更改工作区时，已签发审批、测试和理解证据立即失效。
        if s['expected_snapshot'] != h.sandbox.manifest():
            h.store.transition('WORKSPACE_CHANGED', 'NEEDS_REVIEW', 'WORKSPACE_CHANGED')
            s.update(evidence=[], approvals={}, pending=None)
            for step in s['plan']['steps']:
                step['evidence_ids'] = []
            h.plan.block_all('工作区变化，旧证据失效')
            h.audit.record('WORKSPACE_CHANGED', 'PAUSE', call_id)
            h.checkpoint.save(refresh=False)
            raise Pause()
        if tool == 'write_file':
            normalized = h.sandbox.path(args['path'], write=True).relative_to(h.sandbox.root).as_posix()
            if s['kind'] == 'understand' and not normalized.startswith('artifacts/'):
                return self.reject(call_id, 'TASK_PERMISSION')
            # 测试文件属于验收标准，模型不能通过篡改测试制造“修复成功”。
            if normalized.startswith('tests/') or normalized.split('/')[-1].startswith('test_') or normalized.split('/')[-1] == 'conftest.py':
                return self.reject(call_id, 'TEST_WRITE_DENIED')
            step_id = h.plan.for_tool(tool, args)
            if h.plan.dependencies(step_id):
                return self.reject(call_id, 'PLAN_DEPENDENCY', [{'code': 'PLAN_DEPENDENCY', 'message': '先读取相关源码，再提出修改或报告。'}])
            approval = s['approvals'].get(call_id)
            if not approval or approval != {'signature': signature, 'revision': revision}:
                h.store.transition('APPROVAL_REQUESTED', 'WAITING_APPROVAL')
                s['pending'] = dict(id=call_id, tool=tool, args=args, signature=signature, revision=revision)
                h.audit.record('WRITE_APPROVAL', 'WAIT', call_id, signature=signature, revision=revision)
                h.checkpoint.save()
                raise Pause()
        if tool == 'final_answer':
            completion = h.completion_result()
            if not completion.valid:
                return self.reject(call_id, completion.code, list(completion.missing))
        step_id = h.plan.for_tool(tool, args)
        if h.plan.dependencies(step_id):
            return self.reject(call_id, 'PLAN_DEPENDENCY', [{'code': 'PLAN_DEPENDENCY', 'message': '前置步骤未完成：' + ', '.join(h.plan.dependencies(step_id))}])
        h.plan.begin(step_id)
        # 写入 intent 必须早于副作用；如果结果丢失，恢复时一律停下核对。
        s['ledger'][call_id] = dict(tool=tool, signature=signature, status='EXECUTING')
        h.audit.record('EXECUTION', 'START', call_id, tool=tool, signature=signature)
        h.checkpoint.save()
        try:
            result = h.handle(tool, args, call_id)
        except Exception as exc:
            # 写操作异常也可能已有副作用，不能认定为“什么都没发生”。
            if tool == 'write_file':
                s['ledger'][call_id]['status'] = 'UNKNOWN_RESULT'
                h.store.transition('WRITE_RESULT_UNKNOWN', 'UNKNOWN_RESULT', 'UNKNOWN_RESULT')
                h.audit.record('EXECUTION', 'UNKNOWN', call_id, error=type(exc).__name__)
                h.checkpoint.save(refresh=False)
                raise Pause()
            result = {'ok': False, 'rule': 'HANDLER_ERROR', 'error': type(exc).__name__}
            if tool == 'run_test':
                h.store.add_evidence(kind='test', scope='all', revision=revision, stable=False,
                                     call_id=call_id, step_id=step_id, returncode=None, reason='UNKNOWN_RESULT', output='')
        # 测试可注入“副作用后、result 前”的崩溃；此处不捕获 BaseException。
        if h.after_effect:
            h.after_effect(tool)
        evidence = next((e for e in reversed(s['evidence']) if e['call_id'] == call_id), None)
        success = not (result.get('ok') is False)
        if tool == 'run_test':
            success = result.get('returncode') == 0 and not result.get('reason') and bool(evidence and evidence.get('stable'))
        if step_id == 'inspect':
            # 阅读失败或失败基线不撤销已发生的有效源码阅读；失败事实仍留在 ledger/Audit。
            # 测试文件阅读本身不能冒充源码阅读（路径先规范化）。
            success = any(e['kind'] == 'read' and is_source_path(e['path']) for e in s['evidence'])
        h.plan.observe(step_id, evidence, success, None if success else result.get('reason', '工具结果未满足步骤验收'))
        s['ledger'][call_id].update(status='DONE', result=result)
        s['history'].append(dict(id=call_id, tool=tool, result=result))
        h.audit.record('EXECUTION', 'DONE', call_id, tool=tool, result=result)
        h.checkpoint.save()
        return result

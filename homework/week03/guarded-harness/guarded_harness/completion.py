"""借鉴课程 CompletionContract：返回全部缺项，现场指纹由程序读取。"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
from .plan_store import is_source_path


@dataclass(frozen=True)
class CompletionResult:
    missing: tuple[dict, ...]

    @property
    def valid(self):
        return not self.missing

    @property
    def code(self):
        return self.missing[0]['code'] if self.missing else 'VERIFIED'


def issue(code, message):
    return dict(code=code, message=message)


def latest_test(evidence, scope='all'):
    # 先找最新“尝试”，再判断成功；失败/超时/取消/未知不能被旧通过遮住。
    return next((e for e in reversed(evidence) if e['kind'] == 'test' and e.get('scope', 'all') == scope), None)


def test_issue(evidence, revision):
    latest = latest_test(evidence)
    if not latest:
        return issue('MISSING_CURRENT_TEST', '尚未执行当前版本测试，请调用 run_test。')
    if latest.get('revision') != revision:
        return issue('STALE_TEST', '最新测试不属于当前源码版本，请重新运行 run_test。')
    if latest.get('reason') or latest.get('returncode') != 0:
        return issue('LATEST_TEST_NOT_PASSED', f'最后一次测试未通过或结果未知：{latest.get("reason") or latest.get("returncode")}；必须重新测试。')
    if latest.get('stable') is not True:
        return issue('UNSTABLE_TEST', '测试期间输入发生变化，本次结果不能验收。')
    return None


class BaseContract:
    def shared(self, state):
        missing = []
        if any(v['status'] != 'DONE' for v in state['ledger'].values()):
            missing.append(issue('UNRESOLVED_CALL', '存在结果尚未闭合的调用，需要人工核对。'))
        return missing

    def steps(self, state, required):
        return [issue('PLAN_INCOMPLETE', f'计划步骤 {s["id"]} 尚未完成：{s["status"]}')
                for s in state['plan']['steps'] if s['id'] in required and s['status'] != 'completed']


class RepairContract(BaseContract):
    def verify(self, state, sandbox):
        missing = self.shared(state)
        current = sandbox.manifest(sources_only=True)
        revision = sandbox.revision()
        def acceptance(files):
            return {p: v for p, v in files.items() if p.startswith('tests/') or Path(p).name.startswith('test_') or Path(p).name == 'conftest.py'}
        if acceptance(current) != acceptance(state['initial_sources']):
            missing.append(issue('ACCEPTANCE_TESTS_CHANGED', '验收测试与初始版本不一致，不能靠修改测试完成修复。'))
        writes = [e for e in state['evidence'] if e['kind'] == 'write' and e['path'].endswith('.py')
                  and not e['path'].startswith('artifacts/') and e['revision'] == revision
                  and current.get(e['path']) == e.get('sha256')
                  and current.get(e['path']) != state['initial_sources'].get(e['path'])]
        if not writes:
            missing.append(issue('MISSING_SOURCE_DIFF', '缺少与当前源码内容一致的真实修复证据。'))
        problem = test_issue(state['evidence'], revision)
        if problem:
            missing.append(problem)
        missing += self.steps(state, {'inspect', 'change', 'verify'})
        return CompletionResult(tuple(missing))


class UnderstandingContract(BaseContract):
    def __init__(self, target='artifacts/report.md'):
        self.target = target

    def verify(self, state, sandbox):
        missing = self.shared(state)
        revision = sandbox.revision()
        reads = [e for e in state['evidence'] if e['kind'] == 'read' and e['revision'] == revision
                 and is_source_path(e['path'])]
        if not reads:
            missing.append(issue('MISSING_SOURCE_READ', '请先读取当前版本的相关源码文件。'))
        written = next((e for e in reversed(state['evidence']) if e['kind'] == 'write' and e['path'] == self.target), None)
        target = sandbox.path(self.target)
        if not written:
            missing.append(issue('MISSING_REPORT', f'必须写入约定交付物 {self.target}，其他 Markdown 不能替代。'))
        elif not target.is_file() or not target.read_text().strip():
            missing.append(issue('EMPTY_REPORT', f'报告 {self.target} 不存在或为空。'))
        elif written['revision'] != revision or hashlib.sha256(target.read_bytes()).hexdigest() != written['sha256']:
            missing.append(issue('REPORT_MISMATCH', '报告内容或源码版本与写入证据不匹配，请重新核对交付。'))
        missing += self.steps(state, {'inspect', 'report'})
        return CompletionResult(tuple(missing))


def contract_for(kind):
    return RepairContract() if kind == 'repair' else UnderstandingContract()

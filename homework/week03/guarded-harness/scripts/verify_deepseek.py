"""真实模型验收：只允许自动审批自带示例的那一份确定修复 Diff。"""
from __future__ import annotations
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
import difflib
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

P = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(P))
from guarded_harness.audit import Audit
from guarded_harness.demo import BROKEN, FIXED


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, help='继续本脚本创建的示例运行，路径内应含 repo 与 state')
    args = parser.parse_args()
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    base = (args.base or (P / 'runs' / ('deepseek-' + stamp))).resolve()
    out = P / 'artifacts' / ('deepseek-' + stamp)
    out.mkdir(parents=True)
    transcript = []
    started = time.monotonic()

    def command(action, extra=None):
        argv = [sys.executable, '-m', 'guarded_harness.cli', action, '--workspace', str(base / 'repo')]
        if action != 'init-demo':
            argv += ['--run-dir', str(base / 'state')]
        argv += extra or []
        result = subprocess.run(argv, cwd=P, capture_output=True, text=True, timeout=180)
        transcript.append(dict(command=argv, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr))
        (out / 'transcript.json').write_text(json.dumps(transcript, ensure_ascii=False, indent=2))
        if result.returncode:
            raise RuntimeError(f'{action} 失败，见 {out}/transcript.json')
        return json.loads((base / 'state/checkpoint.json').read_text()) if action != 'init-demo' else None

    if not args.base:
        command('init-demo')
        state = command('run', ['--model', 'deepseek'])
    else:
        state = json.loads((base / 'state/checkpoint.json').read_text())
        # 保存首次 run 的 CLI 原始输出，便于把已有暂停运行接入验收。
        for file in base.glob('initial-*.txt'):
            shutil.copy(file, out / file.name)
    pending = state.get('pending')
    if state['status'] != 'WAITING_APPROVAL' or not pending:
        raise RuntimeError(f'预期审批暂停，实际 {state["status"]}；未自动批准')
    if pending['tool'] != 'write_file' or pending['args'] != {'path': 'calc.py', 'content': FIXED}:
        raise RuntimeError('模型提出的写入超出精确示例 Diff，留在待审批状态，未自动批准')
    assert (base / 'repo/calc.py').read_text() == BROKEN
    shutil.copy(base / 'state/checkpoint.json', out / 'checkpoint-paused.json')
    state = command('approve', ['--call-id', pending['id'], '--actor', 'live-verification-script-exact-fixture'])
    assert state['status'] == 'RUNNING'
    assert (base / 'repo/calc.py').read_text() == FIXED
    shutil.copy(base / 'state/checkpoint.json', out / 'checkpoint-approved.json')
    state = command('resume', ['--model', 'deepseek'])
    for file in ['checkpoint.json', 'audit.jsonl']:
        shutil.copy(base / 'state' / file, out / file)
    records = [json.loads(line) for line in (out / 'audit.jsonl').read_text().splitlines()]
    Audit(out / 'audit.jsonl', state['run_id'])  # 只校验，不追加。
    responses = [r for r in records if r['rule'] == 'MODEL_RESPONSE']
    tests = [r for r in records if r['rule'] == 'EXECUTION' and r['decision'] == 'DONE' and r['tool'] == 'run_test']
    (out / 'source.diff').write_text(''.join(difflib.unified_diff(BROKEN.splitlines(True), FIXED.splitlines(True),
                                                                  fromfile='before/calc.py', tofile='after/calc.py')))
    (out / 'test-results.json').write_text(json.dumps(tests, ensure_ascii=False, indent=2))
    summary = dict(provider='DeepSeek', model='deepseek-flash', thinking='disabled',
                   endpoint='https://api.deepseek.com', generated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                   status=state['status'], stop_reason=state['stop_reason'], run_id=state['run_id'],
                   steps=state['steps'], calls=state['calls'],
                   input_tokens=sum(r['input_tokens'] or 0 for r in responses),
                   output_tokens=sum(r['output_tokens'] or 0 for r in responses),
                   model_responses=len(responses), served_models=sorted({r['served_model'] for r in responses}),
                   model_time_seconds=round(sum(r['elapsed_seconds'] for r in responses), 3),
                   verification_seconds=round(time.monotonic() - started, 3),
                   approval_actor='live-verification-script-exact-fixture',
                   fixture_diff_exactly_matched=True,
                   test_returncodes=[r['result']['returncode'] for r in tests],
                   evidence_dir=str(out.relative_to(P)), workspace=str(base / 'repo'))
    summary['source_sha256'] = {str(f.relative_to(P)): hashlib.sha256(f.read_bytes()).hexdigest()
                                for f in sorted(P.glob('guarded_harness/*.py'))}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    (P / 'artifacts/live-latest.json').write_text(json.dumps({'directory': str(out.relative_to(P))}))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    assert state['status'] == 'COMPLETED', '真实模型未通过最终验收，保留全部证据'
    assert any(r['result']['returncode'] != 0 for r in tests), '缺少修复前失败证据'
    assert any(r['result']['returncode'] == 0 for r in tests), '缺少修复后通过证据'
    assert state['ledger'][pending['id']]['status'] == 'DONE'
    assert len([r for r in records if r['rule'] == 'EXECUTION' and r['decision'] == 'DONE'
                and r['tool_call_id'] == pending['id']]) == 1


if __name__ == '__main__':
    main()

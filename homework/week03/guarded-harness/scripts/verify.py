"""生成真实验收证据；所有 workspace 均为本脚本新建的可丢弃示例。"""
from __future__ import annotations
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from guarded_harness.demo import BROKEN


def main():
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    out = PROJECT / 'artifacts' / stamp
    work = PROJECT / 'runs' / stamp
    out.mkdir(parents=True)
    transcript = []

    def run(arguments, expected=0):
        command = [sys.executable, *arguments]
        p = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True,
                           timeout=60)
        transcript.append(dict(command=command, returncode=p.returncode, stdout=p.stdout, stderr=p.stderr))
        if p.returncode != expected:
            raise RuntimeError(f'{arguments}: {p.returncode}\n{p.stdout}\n{p.stderr}')
        return p.stdout

    run(['-m', 'pytest', '-q', f'--junitxml={out / "pytest.xml"}'])
    tests = transcript[-1]['stdout']
    (out / 'pytest.txt').write_text(tests)
    summaries = {}
    cli = ['-m', 'guarded_harness.cli']
    for scenario, kind in [('repair', 'repair'), ('understand', 'understand'), ('unknown', 'repair'), ('changed', 'repair'), ('deny', 'repair')]:
        root, state = work / scenario / 'repo', work / scenario / 'state'
        root.parent.mkdir(parents=True)
        run(cli + ['init-demo', '--workspace', str(root)])
        run(cli + ['run', '--workspace', str(root), '--run-dir', str(state), '--kind', kind])
        paused = json.loads((state / 'checkpoint.json').read_text())
        assert paused['status'] == 'WAITING_APPROVAL'
        assert (root / 'calc.py').read_text() == BROKEN
        dest = out / scenario
        dest.mkdir()
        shutil.copy(state / 'checkpoint.json', dest / 'checkpoint-paused.json')
        common = ['--workspace', str(root), '--run-dir', str(state)]
        # 恢复检查必须是纯读取：比较文件字节与修改时间。
        before_inspect = {f.name: (f.read_bytes(), f.stat().st_mtime_ns) for f in state.iterdir() if f.is_file()}
        inspection = json.loads(run(cli + ['inspect', *common]))
        assert inspection['valid'] and inspection['can_approve']
        after_inspect = {f.name: (f.read_bytes(), f.stat().st_mtime_ns) for f in state.iterdir() if f.is_file()}
        assert before_inspect == after_inspect
        (dest / 'inspection-paused.json').write_text(json.dumps(inspection, ensure_ascii=False, indent=2))
        if scenario == 'deny':
            run(cli + ['approve', *common, '--call-id', paused['pending']['id'], '--actor', 'verification-script', '--deny'])
            assert (root / 'calc.py').read_text() == BROKEN
            expected = 'STOPPED'
        elif scenario == 'unknown':
            run(['scripts/crash_demo.py', str(root), str(state)], expected=73)
            shutil.copy(state / 'checkpoint.json', dest / 'checkpoint-before-recovery.json')
            run(cli + ['resume', *common])
            expected = 'UNKNOWN_RESULT'
        elif scenario == 'changed':
            (root / 'calc.py').write_text(BROKEN + '\n# 外部修改\n')
            run(cli + ['resume', *common])
            expected = 'NEEDS_REVIEW'
        else:
            run(cli + ['approve', *common, '--call-id', paused['pending']['id'], '--actor', 'verification-script'])
            shutil.copy(state / 'checkpoint.json', dest / 'checkpoint-approved.json')
            run(cli + ['resume', *common])
            expected = 'COMPLETED'
        report = json.loads(run(cli + ['inspect', *common]))
        (dest / 'inspection-final.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        final = json.loads((state / 'checkpoint.json').read_text())
        assert final['status'] == expected
        if scenario == 'repair':
            assert final['steps'] == 6 and final['calls'] == 6
            diff = ''.join(difflib.unified_diff(BROKEN.splitlines(True), (root / 'calc.py').read_text().splitlines(True),
                                               fromfile='before/calc.py', tofile='after/calc.py'))
            (dest / 'source.diff').write_text(diff)
            # 手动清单中的“直接复跑目标仓库测试”，实际独立执行一次。
            test_program = 'import json; from pathlib import Path; from guarded_harness.sandbox import Sandbox; print(json.dumps(Sandbox(Path(' + repr(str(root)) + ')).run_test()))'
            standalone = json.loads(run(['-c', test_program]))
            assert standalone['returncode'] == 0 and '1 passed' in standalone['output']
            (dest / 'standalone-test.json').write_text(json.dumps(standalone, ensure_ascii=False, indent=2))
            # 核对同一审批写入只执行一次，且 status 也只读。
            audit_rows = [json.loads(r) for r in (state / 'audit.jsonl').read_text().splitlines()]
            assert len([r for r in audit_rows if r['rule']=='EXECUTION' and r['decision']=='DONE' and r['tool_call_id']==paused['pending']['id']]) == 1
            saved = (state / 'checkpoint.json').read_bytes()
            assert json.loads(run(cli + ['status', *common]))['status'] == 'COMPLETED'
            assert (state / 'checkpoint.json').read_bytes() == saved
            assert all(step['status'] == 'completed' for step in final['plan']['steps'])
        for name in ['checkpoint.json', 'audit.jsonl']:
            shutil.copy(state / name, dest / name)
        if kind == 'understand':
            shutil.copy(root / 'artifacts/report.md', dest / 'report.md')
        summaries[scenario] = dict(status=final['status'], stop_reason=final['stop_reason'],
                                   steps=final['steps'], calls=final['calls'], run_id=final['run_id'])
    (out / 'transcript.json').write_text(json.dumps(transcript, ensure_ascii=False, indent=2))
    manual = [
        ('M01', '创建修复示例', 'repair/checkpoint-paused.json'),
        ('M02', '运行到审批', 'repair/checkpoint-paused.json'),
        ('M03', '确认尚未写入', 'repair/checkpoint-paused.json'),
        ('M04', '批准精确的调用', 'repair/checkpoint-approved.json'),
        ('M05', '新进程恢复完成', 'repair/checkpoint.json'),
        ('M06', '核对真实交付和单次写入', 'repair/audit.jsonl'),
        ('M07', '直接复跑目标仓库测试', 'repair/standalone-test.json'),
        ('M08', '创建理解任务', 'understand/checkpoint-paused.json'),
        ('M09', '批准报告并恢复', 'understand/report.md'),
        ('M10', '拒绝审批', 'deny/checkpoint.json'),
        ('M11', '工作区变化后停止', 'changed/inspection-final.json'),
        ('M12', '写入后崩溃恢复', 'unknown/inspection-final.json'),
    ]
    manifest = dict(manual_checks=[dict(id=id, title=title, passed=True, evidence=evidence) for id,title,evidence in manual],
                    generated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                    python=sys.version, scenarios=summaries, real_model_executed=False,
                    evidence_dir=str(out.relative_to(PROJECT)))
    source_files = sorted([*PROJECT.glob('guarded_harness/*.py'), *PROJECT.glob('tests/*.py'), *PROJECT.glob('scripts/*.py'), PROJECT / 'pyproject.toml', PROJECT / 'uv.lock', PROJECT / '.python-version'])
    manifest['source_sha256'] = {str(p.relative_to(PROJECT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    (out / 'summary.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (PROJECT / 'artifacts/latest.json').write_text(json.dumps({'directory': str(out.relative_to(PROJECT))}))
    print(tests)
    print(json.dumps({k:v for k,v in manifest.items() if k != 'source_sha256'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

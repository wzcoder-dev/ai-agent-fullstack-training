"""按验收清单逐项执行：原 32 项 + R1–R6。真实模型默认仅核对本次已有证据。"""
from __future__ import annotations
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
from pathlib import Path
import subprocess
import sys

P = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='额外发起 DeepSeek 真实请求；不传则只核对当前源码的最新真实证据')
    args = parser.parse_args()
    def command(argv):
        p = subprocess.run([sys.executable, *argv], cwd=P, capture_output=True, text=True, timeout=180)
        if p.returncode:
            raise RuntimeError(f'{argv}\n{p.stdout}\n{p.stderr}')
        return dict(command=[sys.executable, *argv], returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)
    if args.live:
        command(['scripts/verify_deepseek.py'])
    suite = command(['scripts/verify.py'])
    directory = P / json.loads((P/'artifacts/latest.json').read_text())['directory']
    summary = json.loads((directory/'summary.json').read_text())
    checks = summary['manual_checks'][:]
    selectors = {
        'G1': ['tests/test_gate.py','-k','g1'], 'G2': ['tests/test_gate.py','-k','g2'],
        'G3': ['tests/test_gate.py','-k','g3'], 'G4': ['tests/test_gate.py','-k','g4'],
        'S1': ['tests/test_sandbox.py','-k','s1'], 'S2': ['tests/test_sandbox.py','-k','s2'],
        'S3': ['tests/test_sandbox.py','-k','s3'], 'S4': ['tests/test_sandbox.py','-k','s4 or child_process'],
        'C1': ['tests/test_state_checkpoint.py','-k','c1'], 'C2': ['tests/test_state_checkpoint.py','-k','c2'],
        'C3': ['tests/test_state_checkpoint.py','-k','c3'], 'C4': ['tests/test_state_checkpoint.py','-k','c4'],
        'A1': ['tests/test_audit.py','-k','a1'], 'A2': ['tests/test_audit.py','-k','a2'],
        'H1': ['tests/test_completion.py','-k','h1'], 'H2': ['tests/test_completion.py','-k','h2'],
        'H3': ['tests/test_harness_loop.py','-k','h3'],
        **{f'R{i}': ['tests/test_course_alignment.py','-k',f'r{i}'] for i in range(1,7)},
    }
    for id,selector in selectors.items():
        evidence = command(['-m','pytest','-q',*selector])
        filename = f'check-{id}.json'
        (directory/filename).write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        checks.append(dict(id=id,title='独立运行对应验收用例',passed=True,evidence=filename))
    checks.append(dict(id='T1',title='完整确定性测试集',passed=True,evidence='pytest.txt'))
    # 不以旧源码的模型成功记录为本次升级背书。
    live_dir = P/json.loads((P/'artifacts/live-latest.json').read_text())['directory']
    live = json.loads((live_dir/'summary.json').read_text())
    assert live['status'] == 'COMPLETED' and 0 in live['test_returncodes'] and 1 in live['test_returncodes']
    assert all(hashlib.sha256((P/f).read_bytes()).hexdigest() == sha for f,sha in live['source_sha256'].items())
    checks.append(dict(id='T2',title='当前源码的 DeepSeek 真实模型验收',passed=True,evidence=str(live_dir.relative_to(P)/'summary.json')))
    # 学习总结在 Obsidian；完整验收应保持代码目录不产生学习页面。
    assert not (P/'learning-guide.html').exists()
    assert not (P/'scripts/build_guide.py').exists()
    checks.append(dict(id='M13',title='提交目录不含学习总结及其生成脚本',passed=True,evidence='submission-layout.json'))
    report = dict(generated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), checks=checks,
                  total=len(checks), passed=sum(c['passed'] for c in checks), suite_command=suite['command'])
    (directory/'submission-layout.json').write_text(json.dumps(dict(
        passed=True, absent=['learning-guide.html', 'scripts/build_guide.py']), ensure_ascii=False, indent=2))
    (directory/'checklist.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k != 'checks'}, ensure_ascii=False, indent=2))
    print(directory/'checklist.json')


if __name__ == '__main__':
    main()

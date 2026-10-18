"""命令行入口：审批与继续运行分开，便于观察两个进程之间的恢复。"""
import argparse
import fcntl
import difflib
import json
import os
from pathlib import Path
from .demo import init_workspace, script
from .harness import Harness
from .models import ScriptedModel, live_model
from .checkpoint import Checkpoint, CheckpointError
from .state_store import StateStore
from .sandbox import Sandbox


def main():
    parser = argparse.ArgumentParser(description='Guarded Coding Harness')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init-demo')
    init.add_argument('--workspace', type=Path, required=True)
    for command in ('run', 'resume', 'approve', 'status', 'inspect'):
        p = sub.add_parser(command)
        p.add_argument('--workspace', type=Path, required=True)
        p.add_argument('--run-dir', type=Path, required=True)
        if command == 'run':
            p.add_argument('--kind', choices=['repair', 'understand'], default='repair')
        if command in {'run', 'resume'}:
            p.add_argument('--model', choices=['fake', 'openai', 'deepseek'], default='fake')
            p.add_argument('--max-steps', type=int, default=16)
            p.add_argument('--call-budget', type=int, default=24)
        if command == 'run':
            p.add_argument('--max-tokens', type=int, default=50000)
            p.add_argument('--max-duration', type=float, default=300)
            p.add_argument('--context-max-chars', type=int, default=24000)
            p.add_argument('--keep-recent', type=int, default=2)
        if command == 'approve':
            p.add_argument('--call-id', required=True)
            p.add_argument('--actor', default='human')
            p.add_argument('--deny', action='store_true')
    args = parser.parse_args()
    if args.command in {'init-demo', 'inspect'}:
        return execute(args)
    if args.command == 'status':
        # status 只读已有锁文件；不创建目录、账本或存档。
        lock_path = args.run_dir / '.lock'
        if not lock_path.exists():
            return execute(args)
        with lock_path.open('r') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                parser.exit(2, '该运行正在被另一个进程使用。\n')
            return execute(args)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    # 单个 run 只允许一个 CLI 进程写入，避免双进程重复消费同一审批。
    with (args.run_dir / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(2, '该运行正在被另一个进程使用。\n')
        return execute(args)


def execute(args):
    if args.command == 'init-demo':
        init_workspace(args.workspace)
        print('示例工作区已创建：', args.workspace.resolve())
        return
    if args.command in {'inspect', 'status'}:
        checkpoint = Checkpoint(args.run_dir, StateStore(args.workspace), Sandbox(args.workspace))
        report = checkpoint.inspect()
        result = report if args.command == 'inspect' or not report['valid'] else {
            **json.loads(checkpoint.path.read_text()), 'inspection': report}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    h = Harness(args.workspace, args.run_dir, kind=getattr(args, 'kind', 'repair'),
                resume=args.command != 'run', max_steps=getattr(args, 'max_steps', 16),
                call_budget=getattr(args, 'call_budget', 24),
                max_total_tokens=getattr(args, 'max_tokens', 50000),
                max_duration_seconds=getattr(args, 'max_duration', 300),
                context_max_chars=getattr(args, 'context_max_chars', 24000),
                keep_recent=getattr(args, 'keep_recent', 2))
    if args.command == 'approve':
        h.approve(args.call_id, args.actor, allow=not args.deny)
    elif args.command in {'run', 'resume'}:
        if args.model == 'fake':
            model = ScriptedModel(script(h.store.data['kind']), cursor=h.store.data['steps'])
        else:
            model = live_model(args.model)
        h.run(model, '理解 calc.py 并写报告' if h.store.data['kind'] == 'understand' else '修复 calc.py 使测试通过')
    s = h.store.data
    pending = s['pending']
    if pending:
        target = h.sandbox.path(pending['args']['path'])
        before = target.read_text() if target.exists() else ''
        print('待审批 Diff：')
        print(''.join(difflib.unified_diff(before.splitlines(True), pending['args']['content'].splitlines(True),
                                           fromfile=pending['args']['path'], tofile=pending['args']['path'])))
    print(json.dumps(dict(run_id=s['run_id'], status=s['status'], stop_reason=s['stop_reason'],
                          steps=s['steps'], calls=s['calls'], pending=pending,
                          evidence=s['evidence'], plan=s['plan'], transitions=s['transitions'],
                          tokens_used=s['tokens_used'], elapsed_seconds=s['elapsed_seconds'],
                          resume_inspection=s['resume_inspection'],
                          missing=list(h.completion_result().missing)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

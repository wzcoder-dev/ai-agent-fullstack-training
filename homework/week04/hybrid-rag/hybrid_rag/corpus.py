"""Explicit current-source selection; archived originals and logs never enter ingestion."""
from __future__ import annotations

import csv
import io
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from llama_index.core import Document
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter
from llama_index.core.schema import TextNode

ROOT = Path(__file__).resolve().parents[1]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def log(root: Path, value: dict) -> None:
    path = root / 'artifacts/logs/changes.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(), **value}, ensure_ascii=False) + '\n')


def prepare(root: Path = ROOT, source: Path | None = None) -> dict:
    source = source or root.parents[2] / 'course_code/week04/4-6/rag/corpus'
    if (root / 'corpus/current').exists() or (root / 'corpus/original').exists():
        raise ValueError('corpus already exists; preparation never overwrites existing work')
    shutil.copytree(source, root / 'corpus/original')
    current = root / 'corpus/current'
    for folder in ['docs', 'app', 'tables']:
        shutil.copytree(source / folder, current / folder)
    (current / 'docs/expiry-boundary.md').write_text('''# 登录会话边界

## 结论
登录会话有效条件是 now < expires_at。恰好到期时应返回 False，不能使用 now <= expires_at。
这项结论在现有 session-rules.md 与 app/session.py 中仍成立。
来源：history/2026-09-12-expiry-bug.md 的结论；本次核对现行规则后整理。
''')
    (current / 'docs/logout-requirement.md').write_text('''# 当前退出登录需求（尚未实现）

## 需要改的地方
本需求仍作为待实现方案，不表示代码已完成退出功能。
- app/auth.py：实现 logout()，删除 _TOKEN_STORE 中的令牌记录。
- app/session.py：增加 invalidate(token) 辅助函数，按原需求优先采用此方案。
- tests/test_session.py：增加 test_logout_invalidates。

## 验收标准
退出登录后，再用原令牌调用 check()，必须返回 False。
执行 python -m pytest tests/test_session.py -q，全部通过，不改既有测试期望值。

## 来源与范围
来源：history/2026-10-06-logout-request.md 的需要改的地方与验收标准。
保留待实现需求，不将旧背景时长或未决的服务端缓存清理当作当前规则。
''')
    manifest = {'original_files': sorted(str(p.relative_to(source)) for p in source.rglob('*') if p.is_file()),
                'excluded': {'history/': '原文保留；仍有效结论与需求整理到当前docs并保留来源',
                             'web/': 'v2.4.0历史发布说明', 'pdf/': 'v2.5.0未生效草稿'},
                'derived_sources': {'docs/expiry-boundary.md': 'history/2026-09-12-expiry-bug.md',
                                    'docs/logout-requirement.md': 'history/2026-10-06-logout-request.md'}}
    dump(root / 'corpus/manifest.json', manifest)
    # Keep the teacher questions and assertions intact, with an explicitly adapted gold set.
    questions = json.loads((root.parents[2] / 'course_code/week04/4-4/eval/questions.json').read_text())
    for q in questions['questions']:
        for g in q['gold']:
            if g['file'] == 'history/2026-09-12-expiry-bug.md': g['file'] = 'docs/expiry-boundary.md'
            if g['file'] == 'history/2026-10-06-logout-request.md': g['file'] = 'docs/logout-requirement.md'
    dump(root / 'eval/questions.json', questions)
    log(root, {'action': 'prepare', 'original_count': len(manifest['original_files']), 'reason': '完整保留原始语料；仅当前有效资料入库'})
    return manifest


def load_nodes(root: Path = ROOT) -> tuple[list[TextNode], str]:
    from .chunking import load_nodes as structured_nodes
    return structured_nodes(root)


def ttl(root: Path = ROOT) -> int:
    text = (root / 'corpus/current/docs/session-rules.md').read_text()
    return int(re.search(r'有效期为 (\d+) 分钟', text).group(1))


def update_ttl(minutes: int, root: Path = ROOT) -> list[dict]:
    if minutes <= 0: raise ValueError('minutes must be positive')
    old = ttl(root)
    if old == minutes: return []
    changes = []
    current = root / 'corpus/current'
    for path in sorted(current.rglob('*')):
        if not path.is_file(): continue
        before = path.read_text()
        after = before
        if path.suffix == '.md': after = before.replace(f'{old} 分钟', f'{minutes} 分钟')
        if path.name == 'session.py': after = before.replace(f'{old} * 60', f'{minutes} * 60').replace(str(old * 60), str(minutes * 60))
        if path.suffix == '.csv':
            reader = csv.DictReader(io.StringIO(before))
            rows = list(reader)
            for row in rows:
                if row['服务'] == 'login_demo' and row['会话类型'] == '登录会话':
                    row['有效期'] = f'{minutes} 分钟'
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=reader.fieldnames, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
            after = output.getvalue()
        if after != before:
            path.write_text(after)
            change = {'file': path.relative_to(current).as_posix(), 'before': before, 'after': after,
                      'before_hash': digest(before), 'after_hash': digest(after)}
            changes.append(change)
    questions_path = root / 'eval/questions.json'
    questions = json.loads(questions_path.read_text())
    for q in questions['questions']:
        q['required_facts'] = [x.replace(f'{old} 分钟', f'{minutes} 分钟').replace(str(old * 60), str(minutes * 60)) for x in q['required_facts']]
    dump(questions_path, questions)
    log(root, {'action': 'update_sources', 'reason': '按作业要求更新当前登录有效期', 'old_minutes': old,
               'new_minutes': minutes, 'changes': changes, 'index_status': 'pending_rebuild'})
    return changes

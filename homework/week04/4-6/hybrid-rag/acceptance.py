"""Real-model acceptance: rebuild 30 -> 20, reload in fresh CLI processes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from hybrid_rag.corpus import ROOT, dump, ttl, update_ttl
from hybrid_rag.evaluate import evaluate
from hybrid_rag.models import embedding, load_credentials
from hybrid_rag.retrieval import Search, build, current_dir


def snapshot(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob('*')) if p.is_file()}


def live_query(minutes, env_file, output):
    path = output / f'0{2 if minutes == 30 else 3}-query-{minutes}.json'
    command = [sys.executable, '-m', 'hybrid_rag', 'query', '--model', 'bge', '--answer',
               '--llm-provider', 'deepseek', '--output', str(path)]
    if env_file:
        command += ['--env-file', env_file]
    run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=240)
    if run.returncode:
        # Do not serialize HTTP client diagnostics, which could contain credentials.
        raise RuntimeError(f'fresh-process DeepSeek query failed, exit={run.returncode}')
    result = json.loads(path.read_text())
    assert result['rrf_weights'] == [.7, .3]
    assert all(len(result[mode]) == 3 for mode in ['vector', 'bm25', 'hybrid'])
    records = {n['id']: n for n in json.loads((current_dir('bge') / 'nodes.json').read_text())}
    generation = result['generation']
    assert generation['provider'] == 'deepseek'
    text = generation['answer'].replace('*', '').replace(' ', '')
    assert f'{minutes}分钟' in text
    assert f'{20 if minutes == 30 else 30}分钟' not in text
    refs = {int(x) for x in re.findall(r'\[(\d+)\]', text)}
    assert refs and refs <= {1, 2, 3}
    citations = generation['citations']
    assert [c['id'] for c in citations] == [h['id'] for h in result['hybrid']]
    for citation in citations:
        record = records[citation['id']]
        assert citation['text'] == record['text']
        assert citation['source'] == record['metadata']['file_path']
        assert citation['heading'] == record['metadata']['heading']
    assert any(f'{minutes}分钟' in citations[i - 1]['text'].replace(' ', '') for i in refs)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file')
    args = parser.parse_args()
    os.environ['BGE_BACKEND'] = 'torch'
    load_credentials(args.env_file)
    assert ttl() == 20, 'acceptance expects the final current source at 20 minutes'
    output = ROOT / 'artifacts/logs/acceptance'
    output.mkdir(parents=True, exist_ok=True)
    originals = snapshot(ROOT / 'corpus/original')
    current = snapshot(ROOT / 'corpus/current')
    questions = (ROOT / 'eval/questions.json').read_bytes()
    course = ROOT.parents[3] / 'course_code/week04/4-6/rag/corpus'
    assert originals == snapshot(course) and len(originals) == 12
    model = embedding('bge')
    success = False
    try:
        update_ttl(30)
        before = build('bge', model)
        # Use a relevant source sample in the screenshot rather than an arbitrary first node.
        before['rule_sample'] = next(n for n in json.loads((current_dir('bge') / 'nodes.json').read_text())
                                     if n['metadata']['file_path'] == 'docs/session-rules.md'
                                     and n['metadata']['heading'].endswith('有效期长度'))
        dump(output / '01-build-30.json', before)
        query30 = live_query(30, args.env_file, output)
        assert query30['version'] == before['version']
        changes = update_ttl(20)
        stale_rejected = False
        try:
            Search('bge', model)
        except ValueError as error:
            if 'sources changed' not in str(error):
                raise
            stale_rejected = True
        assert stale_rejected
        after = build('bge', model)
        search = Search('bge', model)
        retired = set(before['node_ids']) - set(after['node_ids'])
        assert retired
        active = {n['id'] for n in search.records}
        vector_ids = set(search.index.vector_store.data.embedding_dict)
        assert active == vector_ids == set(after['node_ids'])
        assert not retired & active
        assert not any(re.search(r'(?<!\d)30\s*分钟|30\s*\*\s*60|\b1800\b', n['text']) for n in search.records)
        query20 = live_query(20, args.env_file, output)
        assert query20['version'] == after['version']
        assert all(not retired & {h['id'] for h in query20[mode]}
                   for mode in ['vector', 'bm25', 'hybrid'])
        report = evaluate(search)
        dump(output / '04-eight-questions.json', report)
        assert all(report['summary']['hybrid'][k] == .875 for k in ['recall', 'hit', 'complete_hit'])
        assert originals == snapshot(ROOT / 'corpus/original') == snapshot(course)
        assert current == snapshot(ROOT / 'corpus/current')
        assert questions == (ROOT / 'eval/questions.json').read_bytes()
        result = {'status': 'passed', 'time_utc': datetime.now(timezone.utc).isoformat(),
                  'provider': 'deepseek', 'generation_model': query20['generation']['model'],
                  'embedding_model': after['embedding_model'], 'rrf_weights': [.7, .3],
                  'candidate_k': 20, 'top_k': 3, 'nodes': after['nodes'],
                  'before_version': before['version'], 'after_version': after['version'],
                  'ttl_before': 30, 'ttl_after': 20, 'changed_files': [c['file'] for c in changes],
                  'retired_node_ids': sorted(retired), 'retired_count': len(retired),
                  'old_nodes_absent_from_both_paths': True, 'stale_index_rejected': True,
                  'fresh_process_answers_verified': True, 'citations_match_persisted_nodes': True,
                  'original_files': 12, 'originals_byte_identical_to_course': True,
                  'final_current_sources_unchanged': True,
                  'metrics': {mode: {k: report['summary'][mode][k] for k in ['recall', 'hit', 'complete_hit']}
                              for mode in ['vector', 'bm25', 'hybrid']},
                  'screenshots': [], 'build_20': after}
        dump(output / 'summary.json', result)
        success = True
        print(json.dumps({k: v for k, v in result.items() if k != 'build_20'}, ensure_ascii=False, indent=2))
    finally:
        if not success:
            update_ttl(20)
            build('bge', model)
            dump(output / 'failure.json', {'status': 'failed', 'restored_ttl': ttl(),
                                           'restored_version': current_dir('bge').name})


if __name__ == '__main__':
    main()

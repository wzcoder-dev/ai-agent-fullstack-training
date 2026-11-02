"""Teacher-aligned gold retrieval metrics, without an LLM judge."""
from __future__ import annotations

import json
from pathlib import Path

from .corpus import ROOT, dump
from .retrieval import Search


def matches(hit: dict, gold: dict) -> bool:
    return hit['source'] == gold['file'] and (not gold.get('heading') or gold['heading'] in hit['heading'])


def metrics(hits: list[dict], gold: list[dict]) -> dict:
    found = sum(any(matches(h, g) for h in hits) for g in gold)
    relevant_hits = sum(any(matches(h, g) for g in gold) for h in hits)
    return {'recall': found / len(gold) if gold else None,
            'gold_precision': relevant_hits / len(hits) if hits and gold else None,
            'hit': int(found > 0) if gold else None,
            'complete_hit': int(found == len(gold)) if gold else None}


def evaluate(search: Search, root: Path = ROOT) -> dict:
    questions = json.loads((root / 'eval/questions.json').read_text())['questions']
    rows = []
    for question in questions:
        result = search.query(question['question'])
        rows.append({'id': question['id'], 'gold': question['gold'], 'expected': question['required_facts'],
                     'retrieval': result,
                     'metrics': {mode: metrics(result[mode], question['gold']) for mode in ['vector', 'bm25', 'hybrid']}})
    summary = {}
    for mode in ['vector', 'bm25', 'hybrid']:
        summary[mode] = {key: round(sum(r['metrics'][mode][key] for r in rows) / len(rows), 4)
                         for key in ['recall', 'gold_precision', 'hit', 'complete_hit']}
    return {'model': search.manifest['model'], 'embedding_model': search.manifest['embedding_model'],
            'embedding_backend': search.manifest['embedding_backend'],
            'version': search.manifest['version'], 'source_hash': search.manifest['source_hash'],
            'dimensions': search.manifest['dimensions'], 'build_seconds': search.manifest['build_seconds'],
            'mean_query_seconds': round(sum(r['retrieval']['query_seconds'] for r in rows) / len(rows), 4),
            'questions': len(rows), 'top_k': 3,
            'metric_note': 'gold_precision仅按课程指定gold定位计算；gold不穷举所有可回答的替代片段，低分不等于实际证据无关。检索指标不等于答案正确率。',
            'summary': summary, 'rows': rows}


def comparison(reports: list[dict]) -> str:
    if len({r['source_hash'] for r in reports}) != 1:
        raise ValueError('cannot compare different source versions')
    lines = ['# 八题检索对比（真实 Embedding）', '',
             '| 模型 | 方式 | Recall@3 | Gold Precision@3 | Hit@3 | 完全命中率@3 |',
             '| --- | --- | --- | --- | --- | --- |']
    if any(r['summary']['bm25'] != reports[0]['summary']['bm25'] for r in reports):
        raise ValueError('shared BM25 baselines disagree; check the node sets and evaluation settings')
    rows = [(r['model'], 'vector', r['summary']['vector']) for r in reports]
    rows.append(('共享基线', 'bm25', reports[0]['summary']['bm25']))
    rows.extend((r['model'], 'hybrid', r['summary']['hybrid']) for r in reports)
    for model, mode, scores in rows:
        lines.append('| ' + ' | '.join([model, mode] + [str(scores[k]) for k in ['recall', 'gold_precision', 'hit', 'complete_hit']]) + ' |')
    lines += ['', reports[0]['metric_note'], '', '八题是小样本、已知课程题，结果不能推广为模型总体排名。耗时包含本次调用与初始化影响，不是稳定性能基准。', '',
              '| 模型 | 维度 | 建库秒 | 平均三路查询秒 |', '| --- | --- | --- | --- |']
    for r in reports: lines.append(f'| {r["model"]} | {r["dimensions"]} | {r["build_seconds"]} | {r["mean_query_seconds"]} |')
    return '\n'.join(lines) + '\n'

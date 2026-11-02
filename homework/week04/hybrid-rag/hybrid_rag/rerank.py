"""Run a controlled rerank comparison on the saved RRF candidate lists."""
from __future__ import annotations

import argparse
import json
import os
import time

import requests

from .corpus import ROOT, dump
from .evaluate import matches
from .models import load_credentials
from .retrieval import current_dir


class RerankUnavailable(RuntimeError):
    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(f'rerank unavailable: HTTP {status}, code {code}')


def rerank(question, hits, top_n=3):
    if not hits: return []
    key = os.getenv('ZHIPUAI_API_KEY')
    if not key: raise ValueError('missing ZHIPUAI_API_KEY')
    response = requests.post('https://open.bigmodel.cn/api/paas/v4/rerank',
        headers={'Authorization': f'Bearer {key}'},
        json={'model': 'rerank', 'query': question,
              'documents': [h['text'] for h in hits], 'top_n': min(top_n, len(hits))}, timeout=30)
    if not response.ok:
        try: code = response.json().get('error', {}).get('code')
        except ValueError: code = None
        raise RerankUnavailable(response.status_code, code)
    results = response.json().get('results', [])
    if len(results) != min(top_n, len(hits)):
        raise ValueError('rerank returned an incomplete result set')
    seen = set()
    ranked = []
    for item in results:
        i = item.get('index')
        if type(i) is not int or i < 0 or i >= len(hits) or i in seen:
            raise ValueError('rerank returned invalid or duplicate candidate index')
        score = float(item['relevance_score'])
        import math
        if not math.isfinite(score): raise ValueError('rerank returned invalid score')
        seen.add(i)
        ranked.append({**hits[i], 'rrf_score': hits[i]['score'], 'score': score, 'score_type': 'rerank'})
    return sorted(ranked, key=lambda h: -h['score'])


def three_metrics(hits, gold):
    found = sum(any(matches(h, g) for h in hits) for g in gold)
    return {'recall': found / len(gold), 'hit': int(found > 0), 'complete_hit': int(found == len(gold))}


class LocalReranker:
    """Independent cross encoder; it never calls an API or changes embeddings."""
    def __init__(self, device='cpu'):
        import torch
        from sentence_transformers import CrossEncoder
        path = ROOT / '.model-cache/bge-reranker-base'
        if not (path / 'model.safetensors').is_file():
            raise ValueError('run download_bge.py --reranker first')
        if device == 'mps' and not torch.backends.mps.is_available():
            raise ValueError('MPS is unavailable')
        torch.set_num_threads(4)
        self.device = device
        self.engine = CrossEncoder(str(path), device=device, max_length=512,
                                   trust_remote_code=False, local_files_only=True)
        self.max_pair_tokens = 0

    def __call__(self, question, hits, top_n=3):
        if not hits: return []
        pairs = [(question, h['text']) for h in hits]
        encoded = self.engine.tokenizer([p[0] for p in pairs], [p[1] for p in pairs],
                                        truncation=False, padding=False)
        maximum = max(len(ids) for ids in encoded['input_ids'])
        self.max_pair_tokens = max(self.max_pair_tokens, maximum)
        if maximum > 512: raise ValueError('rerank pair exceeds 512 tokens; refusing silent truncation')
        scores = self.engine.predict(pairs, batch_size=4, show_progress_bar=False, convert_to_numpy=True)
        import math
        if not all(math.isfinite(float(s)) for s in scores): raise ValueError('invalid local rerank score')
        order = sorted(range(len(hits)), key=lambda i: (-float(scores[i]), hits[i]['id']))[:top_n]
        return [{**hits[i], 'rrf_score': hits[i]['score'], 'score': float(scores[i]),
                 'score_type': 'cross_encoder'} for i in order]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file')
    parser.add_argument('--provider', choices=['zhipu', 'local'], default='zhipu')
    parser.add_argument('--device', choices=['cpu', 'mps'], default='cpu')
    args = parser.parse_args()
    if args.provider == 'zhipu': load_credentials(args.env_file)
    started = time.perf_counter()
    ranker = LocalReranker(args.device) if args.provider == 'local' else rerank
    load_seconds = time.perf_counter() - started
    saved = json.loads((ROOT / 'artifacts/logs/rerank-candidate-diagnosis.json').read_text())
    questions = {q['id']: q for q in json.loads((ROOT / 'eval/questions.json').read_text())['questions']}
    report = {'status': 'running', 'model': 'BAAI/bge-reranker-base' if args.provider == 'local' else 'rerank',
              'provider': args.provider, 'device': args.device if args.provider == 'local' else 'remote',
              'load_seconds': load_seconds, 'candidate_k': 20, 'top_k': 3, 'models': {}}
    output = ROOT / 'artifacts/logs' / ('local-rerank-comparison.json' if args.provider == 'local' else 'rerank-comparison.json')
    try:
        for name, rows in saved['models'].items():
            manifest = json.loads((current_dir(name) / 'manifest.json').read_text())
            details = []
            for row in rows:
                candidates = row['candidates']
                if candidates['version'] != manifest['version']:
                    raise ValueError('saved candidates are stale; refresh the candidate diagnosis')
                q = questions[row['id']]
                if q['question'] != row['question'] or q['gold'] != [p['gold'] for p in row['gold_probes']]:
                    raise ValueError('question or gold changed since candidate collection')
                hits = candidates['hybrid']
                start = time.perf_counter()
                ranked = ranker(q['question'], hits)
                details.append({'id': q['id'], 'rrf': three_metrics(hits[:3], q['gold']),
                                'rerank': three_metrics(ranked, q['gold']), 'hits': ranked,
                                'rerank_seconds': time.perf_counter() - start})
                print(f'{name} {q["id"]}: {details[-1]["rerank"]}', flush=True)
            summary = {mode: {k: sum(r[mode][k] for r in details) / len(details)
                              for k in ['recall', 'hit', 'complete_hit']} for mode in ['rrf', 'rerank']}
            report['models'][name] = {'summary': summary, 'rows': details}
            dump(output, report)
        report['status'] = 'success'
        if args.provider == 'local': report['max_pair_tokens'] = ranker.max_pair_tokens
    except RerankUnavailable as error:
        report.update(status='blocked', http_status=error.status, provider_code=error.code)
        dump(output, report)
        print(json.dumps({'status': 'blocked', 'http_status': error.status, 'provider_code': error.code}))
        raise SystemExit(1)
    dump(output, report)
    print(json.dumps({name: value['summary'] for name, value in report['models'].items()}, ensure_ascii=False))


if __name__ == '__main__': main()

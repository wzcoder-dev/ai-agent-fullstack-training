from __future__ import annotations

import argparse
import json
from pathlib import Path

from .corpus import ROOT, dump, load_nodes, prepare, ttl, update_ttl
from .evaluate import comparison, evaluate
from .models import answer, embedding, load_credentials
from .retrieval import Search, build, current_dir


def main():
    parser = argparse.ArgumentParser(description='Week04 hybrid RAG homework')
    parser.add_argument('command', choices=['prepare', 'build', 'query', 'evaluate', 'compare', 'update'])
    parser.add_argument('--model', choices=['bge', 'zhipu'], default='bge')
    parser.add_argument('--models', nargs='+', choices=['bge', 'zhipu'], default=['bge', 'zhipu'])
    parser.add_argument('--env-file', help='explicit credential dotenv file; contents are never printed')
    parser.add_argument('--question', default='登录会话的有效期是多少分钟？')
    parser.add_argument('--answer', action='store_true', help='generate a cited answer from hybrid evidence (default: DeepSeek)')
    parser.add_argument('--llm-provider', choices=['deepseek', 'zhipu'])
    parser.add_argument('--llm-model')
    parser.add_argument('--minutes', type=int, default=20)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    load_credentials(args.env_file)
    if args.command == 'prepare': result = prepare()
    elif args.command == 'build': result = build(args.model, embedding(args.model))
    elif args.command == 'query':
        result = Search(args.model, embedding(args.model)).query(args.question)
        if args.answer: result['generation'] = answer(args.question, result['hybrid'], args.llm_provider, args.llm_model)
    elif args.command == 'evaluate': result = evaluate(Search(args.model, embedding(args.model)))
    elif args.command == 'compare':
        reports = []
        for name in args.models:
            report = evaluate(Search(name, embedding(name)))
            dump(ROOT / f'artifacts/logs/{name}-{ttl()}min-evaluation.json', report)
            reports.append(report)
        text = comparison(reports)
        path = ROOT / f'artifacts/logs/comparison-{ttl()}min.md'
        path.write_text(text)
        print(text)
        return
    else:
        previous = {}
        for name in args.models:
            path = current_dir(name) / 'manifest.json'
            previous[name] = json.loads(path.read_text())
        changes = update_ttl(args.minutes)
        current_ids = {n.node_id for n in load_nodes()[0]}
        builds = []
        for name in args.models:
            model = embedding(name)
            manifest = build(name, model)
            retired = set(previous[name]['node_ids']) - current_ids
            index = Search(name, model)
            if retired & set(index.manifest['node_ids']): raise ValueError('retired nodes remain')
            builds.append({'model': name, 'version': manifest['version'], 'retired_nodes': len(retired),
                           'retired_absent': True, 'query': index.query(args.question)})
        result = {'changes': [c['file'] for c in changes], 'ttl_minutes': ttl(), 'builds': builds}
    if args.output: dump(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

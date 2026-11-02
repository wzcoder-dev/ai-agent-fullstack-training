"""Download the official ModelScope BGE mirror, verify every file by its SHA256.

Fallback for blocked HuggingFace ONNX CDN. Only safetensors/JSON/tokenizer files;
no remote Python code or pickle weights are loaded.
"""
from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path

import requests

from hybrid_rag.corpus import ROOT, dump


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reranker', action='store_true', help='download BAAI/bge-reranker-base instead')
    args = parser.parse_args()
    name = 'BAAI/bge-reranker-base' if args.reranker else 'AI-ModelScope/bge-small-zh-v1.5'
    base = f'https://modelscope.cn/api/v1/models/{name}/repo'
    response = requests.get(base + '/files', params={'Revision': 'master', 'Recursive': 'true'}, timeout=30)
    response.raise_for_status()
    files = response.json()['Data']['Files']
    target = ROOT / '.model-cache' / ('bge-reranker-base' if args.reranker else 'bge-sentence-transformers')
    target.mkdir(parents=True, exist_ok=True)
    evidence = []
    for item in files:
        name = item['Path']
        if not (name.endswith(('.json', '.txt', '.safetensors')) or name == 'sentencepiece.bpe.model'): continue
        path = (target / name).resolve()
        if not path.is_relative_to(target.resolve()): raise ValueError('invalid repository path')
        expected = item['Sha256']
        if not expected: raise ValueError('missing checksum')
        if not path.exists() or checksum(path) != expected:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + '.download')
            failures = 0
            while not temporary.exists() or temporary.stat().st_size < item['Size']:
                offset = temporary.stat().st_size if temporary.exists() else 0
                end = min(offset + 8 * 1024 * 1024, item['Size']) - 1
                try:
                    with requests.get(base, params={'Revision': 'master', 'FilePath': name},
                                      headers={'Range': f'bytes={offset}-{end}'}, stream=True, timeout=(15, 60)) as request:
                        request.raise_for_status()
                        if request.status_code == 206 and not request.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                            raise ValueError('invalid partial response offset')
                        mode = 'ab' if request.status_code == 206 and offset else 'wb'
                        with temporary.open(mode) as stream:
                            for chunk in request.iter_content(1024 * 1024): stream.write(chunk)
                    if name.endswith('.safetensors'):
                        print(f'weight download {temporary.stat().st_size}/{item["Size"]} bytes', flush=True)
                except requests.RequestException:
                    failures += 1
                    if failures >= 6: raise
            if checksum(temporary) != expected:
                raise ValueError(f'checksum mismatch: {name}')
            temporary.replace(path)
        evidence.append({'file': name, 'sha256': expected, 'bytes': path.stat().st_size})
        print(name, path.stat().st_size, flush=True)
    record = 'bge-reranker-download.json' if args.reranker else 'bge-model-download.json'
    dump(ROOT / 'artifacts/logs' / record, {'source': base, 'files': evidence, 'format': 'safetensors'})


if __name__ == '__main__': main()

"""Build vector and BM25 snapshots together, publish only completed versions."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import jieba
from llama_index.core import StorageContext, VectorStoreIndex, load_index_from_storage
from llama_index.core.schema import TextNode
from rank_bm25 import BM25Okapi

from .corpus import ROOT, dump, load_nodes, log, ttl

jieba.setLogLevel(40)
DEFAULT_RRF_WEIGHTS = (0.7, 0.3)  # vector, BM25; chosen from the course eight-question comparison


def tokenize(text):
    return [x for x in jieba.lcut(text.lower()) if x.strip()]


def node_record(node):
    return {'id': node.node_id, 'text': node.text, 'metadata': node.metadata}


def build(name: str, model, root: Path = ROOT) -> dict:
    started = time.perf_counter()
    nodes, source_hash = load_nodes(root)
    version = f'{source_hash[:12]}-{uuid.uuid4().hex[:8]}'
    directory = root / 'storage' / name / 'versions' / version
    directory.mkdir(parents=True)
    try:
        index = VectorStoreIndex(nodes, embed_model=model, show_progress=False)
        index.storage_context.persist(str(directory / 'vector'))
        # Safe JSON, no executable pickle. BM25 is reconstructed from this exact snapshot.
        records = [node_record(n) for n in nodes]
        dump(directory / 'nodes.json', records)
        bm25 = BM25Okapi([tokenize(n.text) for n in nodes])
        dump(directory / 'bm25.json', {'node_ids': [n.node_id for n in nodes], 'tokens': bm25.corpus_size})
        dimensions = len(next(iter(index.vector_store.data.embedding_dict.values())))
        manifest = {'version': version, 'model': name, 'embedding_model': model.model_name,
                    'embedding_backend': getattr(model, 'backend', 'provider'),
                    'dimensions': dimensions, 'source_hash': source_hash, 'nodes': len(nodes),
                    'ttl_minutes': ttl(root), 'build_seconds': round(time.perf_counter() - started, 3),
                    'sample': records[0], 'node_ids': [n.node_id for n in nodes]}
        dump(directory / 'manifest.json', manifest)
        # Validate loadability before replacing the single pointer shared by both paths.
        load_index_from_storage(StorageContext.from_defaults(persist_dir=str(directory / 'vector')), embed_model=model)
        pointer = directory.parents[1] / f'.CURRENT-{uuid.uuid4().hex}.json'
        dump(pointer, {'version': version})
        os.replace(pointer, directory.parents[1] / 'CURRENT.json')
        log(root, {'action': 'build', 'status': 'success', 'model': name, 'version': version,
                   'source_hash': source_hash, 'nodes': len(nodes), 'build_seconds': manifest['build_seconds']})
        return manifest
    except Exception as error:
        log(root, {'action': 'build', 'status': 'failure', 'model': name, 'version': version,
                   'source_hash': source_hash, 'error_type': type(error).__name__})
        raise


def current_dir(name: str, root: Path = ROOT) -> Path:
    version = json.loads((root / 'storage' / name / 'CURRENT.json').read_text())['version']
    return root / 'storage' / name / 'versions' / version


def fuse(vector, bm25, top_k=3, constant=60, weights=(1.0, 1.0)):
    import math
    if len(weights) != 2 or any(not math.isfinite(w) or w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError('RRF weights must be two finite nonnegative numbers with a positive sum')
    scores = {}
    keep = {}
    for weight, hits in zip(weights, (vector, bm25)):
        for rank, hit in enumerate(hits, 1):
            key = hit['id']
            scores[key] = scores.get(key, 0) + weight / (constant + rank)
            keep[key] = hit
    order = sorted(scores, key=lambda key: (-scores[key], key))[:top_k]
    return [{**keep[key], 'score': scores[key], 'score_type': 'rrf'} for key in order]


class Search:
    def __init__(self, name, model, root: Path = ROOT):
        self.model = model
        directory = current_dir(name, root)
        self.manifest = json.loads((directory / 'manifest.json').read_text())
        if self.manifest['source_hash'] != load_nodes(root)[1]:
            raise ValueError('sources changed; rebuild both retrieval paths before querying')
        if self.manifest['embedding_model'] != model.model_name:
            raise ValueError('embedding model does not match stored index')
        if self.manifest['embedding_backend'] != getattr(model, 'backend', 'provider'):
            raise ValueError('embedding backend does not match stored index')
        self.index = load_index_from_storage(StorageContext.from_defaults(persist_dir=str(directory / 'vector')), embed_model=model)
        self.records = json.loads((directory / 'nodes.json').read_text())
        self.bm25 = BM25Okapi([tokenize(n['text']) for n in self.records])
        ids = [n['id'] for n in self.records]
        if set(ids) != set(self.index.vector_store.data.embedding_dict):
            raise ValueError('vector and BM25 node sets disagree')
        if ids != json.loads((directory / 'bm25.json').read_text())['node_ids']:
            raise ValueError('BM25 snapshot does not match nodes')

    def query(self, question: str, top_k=3, candidate_k=20, rrf_weights=DEFAULT_RRF_WEIGHTS):
        start = time.perf_counter()
        count = min(max(top_k, candidate_k), len(self.records))
        hits = self.index.as_retriever(similarity_top_k=count).retrieve(question)
        vector = [{'id': h.node.node_id, 'text': h.node.text, 'source': h.node.metadata['file_path'],
                   'heading': h.node.metadata['heading'], 'score': float(h.score or 0), 'score_type': 'cosine'} for h in hits]
        scores = self.bm25.get_scores(tokenize(question))
        order = sorted(range(len(scores)), key=lambda i: (-scores[i], self.records[i]['id']))[:count]
        bm25 = [{'id': self.records[i]['id'], 'text': self.records[i]['text'],
                 'source': self.records[i]['metadata']['file_path'], 'heading': self.records[i]['metadata']['heading'],
                 'score': float(scores[i]), 'score_type': 'bm25'} for i in order]
        return {'question': question, 'version': self.manifest['version'], 'model': self.manifest['model'],
                'vector': vector[:top_k], 'bm25': bm25[:top_k],
                'hybrid': fuse(vector, bm25, top_k, weights=rrf_weights),
                'rrf_weights': list(rrf_weights), 'candidate_k': candidate_k, 'top_k': top_k,
                'query_seconds': round(time.perf_counter() - start, 4)}

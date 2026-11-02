from pathlib import Path

import pytest
from llama_index.core.embeddings import MockEmbedding

from hybrid_rag.corpus import ROOT, load_nodes, prepare, ttl, update_ttl
from hybrid_rag.evaluate import metrics
from hybrid_rag.retrieval import Search, build, fuse


@pytest.fixture
def corpus(tmp_path):
    import shutil
    shutil.copytree(ROOT / 'corpus/current', tmp_path / 'corpus/current')
    shutil.copytree(ROOT / 'eval', tmp_path / 'eval')
    # The real working copy may already be updated; reset only this test fixture.
    update_ttl(30, tmp_path)
    return tmp_path


def test_update_preserves_other_sessions_and_original_archive(corpus):
    before = (corpus / 'corpus/current/tables/session-matrix.csv').read_text()
    changes = update_ttl(20, corpus)
    after = (corpus / 'corpus/current/tables/session-matrix.csv').read_text()
    assert ttl(corpus) == 20
    assert 'pay_gateway,支付会话,5 分钟' in after
    assert '匿名浏览会话,120 分钟' in after
    assert '记住我令牌,30 天' in after
    assert '登录会话,20 分钟' in after
    assert '登录会话,30 分钟' in before
    assert any(c['file'] == 'docs/testing-guide.md' for c in changes)
    assert '20 * 60' in (corpus / 'corpus/current/app/session.py').read_text()
    assert update_ttl(20, corpus) == []


def test_persist_reload_update_and_failed_build_keep_last_pointer(corpus):
    model = MockEmbedding(embed_dim=8)
    first = build('test', model, corpus)
    search = Search('test', model, corpus)
    assert search.query('登录有效期')['hybrid']
    update_ttl(20, corpus)
    with pytest.raises(ValueError, match='sources changed'): Search('test', model, corpus)
    second = build('test', model, corpus)
    retired = set(first['node_ids']) - set(second['node_ids'])
    assert retired
    search = Search('test', model, corpus)
    assert not retired & set(search.manifest['node_ids'])
    assert search.manifest['ttl_minutes'] == 20

    class BrokenEmbedding(MockEmbedding):
        def _get_text_embeddings(self, texts): raise RuntimeError('simulated provider failure')

    with pytest.raises(RuntimeError): build('test', BrokenEmbedding(embed_dim=8), corpus)
    assert Search('test', model, corpus).manifest['version'] == second['version']


def test_nodes_deterministic_and_logs_not_ingested(corpus):
    nodes, version = load_nodes(corpus)
    (corpus / 'artifacts/logs').mkdir(parents=True, exist_ok=True)
    (corpus / 'artifacts/logs/old.txt').write_text('登录有效期30分钟')
    again, same = load_nodes(corpus)
    assert version == same
    assert [n.node_id for n in nodes] == [n.node_id for n in again]
    assert all(not n.metadata['file_path'].startswith(('history/', 'pdf/', 'web/')) for n in nodes)


def test_rrf_deduplicates_and_does_not_add_raw_scores():
    a = {'id': 'a', 'score': 1000000}
    b = {'id': 'b', 'score': -999}
    result = fuse([a, b], [b, a])
    assert len(result) == 2
    assert result[0]['score'] == pytest.approx(1 / 61 + 1 / 62)


def test_weighted_rrf_prefers_selected_retrieval_path():
    a = {'id': 'a', 'score': -999}
    b = {'id': 'b', 'score': 999999}
    assert fuse([a, b], [b, a], weights=(.7, .3))[0]['id'] == 'a'
    assert fuse([a, b], [b, a], weights=(.3, .7))[0]['id'] == 'b'
    with pytest.raises(ValueError): fuse([a], [b], weights=(float('nan'), 1))


def test_structured_chunks_keep_facts_and_complete_functions(corpus):
    import ast
    import re
    from hybrid_rag.chunking import tools
    tokenizer, _ = tools()
    nodes, _ = load_nodes(corpus)
    assert all(len(tokenizer.encode(n.text).ids) <= 512 for n in nodes)
    assert not any(all(re.match(r'^#{1,6}\s+', line.strip())
                       for line in n.text.splitlines() if line.strip()) for n in nodes)
    for path in (corpus / 'corpus/current/app').glob('*.py'):
        source = path.read_text()
        for item in ast.parse(source).body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert any(ast.get_source_segment(source, item) in n.text for n in nodes)
    assert any('SESSION_TTL_SECONDS = 30 * 60' in n.text for n in nodes)
    assert any('登录会话有效期规则 > 有效期长度' in n.text for n in nodes)


def test_gold_metrics_distinguish_partial_and_complete():
    gold = [{'file': 'a', 'heading': 'x'}, {'file': 'b', 'heading': 'y'}]
    scores = metrics([{'source': 'a', 'heading': 'x'}, {'source': 'z', 'heading': ''}], gold)
    assert scores == {'recall': .5, 'gold_precision': .5, 'hit': 1, 'complete_hit': 0}

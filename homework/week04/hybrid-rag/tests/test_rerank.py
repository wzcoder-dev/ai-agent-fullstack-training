import pytest

from hybrid_rag.rerank import LocalReranker, RerankUnavailable, rerank


def response(status, data):
    class Response:
        ok = status == 200
        status_code = status
        def json(self): return data
    return Response()


def test_provider_failure_is_not_reported_as_rrf_fallback(monkeypatch):
    monkeypatch.setenv('ZHIPUAI_API_KEY', 'test-only')
    monkeypatch.setattr('hybrid_rag.rerank.requests.post', lambda *a, **k: response(429, {'error': {'code': '1113'}}))
    with pytest.raises(RerankUnavailable) as error: rerank('question', [{'text': 'evidence'}])
    assert error.value.code == '1113'


def test_mapping_preserves_sources_and_rejects_duplicate_indices(monkeypatch):
    monkeypatch.setenv('ZHIPUAI_API_KEY', 'test-only')
    hits = [{'id': 'a', 'source': 'a.md', 'text': 'a', 'score': .03},
            {'id': 'b', 'source': 'b.md', 'text': 'b', 'score': .02}]
    monkeypatch.setattr('hybrid_rag.rerank.requests.post', lambda *a, **k: response(200, {'results': [
        {'index': 1, 'relevance_score': .9}, {'index': 0, 'relevance_score': .1}]}))
    ranked = rerank('question', hits)
    assert [h['source'] for h in ranked] == ['b.md', 'a.md']
    assert ranked[0]['rrf_score'] == .02
    assert hits[0]['score'] == .03
    monkeypatch.setattr('hybrid_rag.rerank.requests.post', lambda *a, **k: response(200, {'results': [
        {'index': 0, 'relevance_score': .9}, {'index': 0, 'relevance_score': .1}]}))
    with pytest.raises(ValueError, match='duplicate'): rerank('question', hits)


def test_local_ranker_checks_token_limit_and_preserves_evidence():
    class Engine:
        def tokenizer(self, queries, docs, **kwargs): return {'input_ids': [[1, 2] for _ in docs]}
        def predict(self, pairs, **kwargs): return [.1, .9]
    ranker = object.__new__(LocalReranker)
    ranker.engine = Engine()
    ranker.max_pair_tokens = 0
    hits = [{'id': 'a', 'source': 'a.md', 'text': 'a', 'score': .03},
            {'id': 'b', 'source': 'b.md', 'text': 'b', 'score': .02}]
    assert [h['id'] for h in ranker('q', hits)] == ['b', 'a']
    assert ranker.max_pair_tokens == 2
    ranker.engine.tokenizer = lambda *a, **k: {'input_ids': [[1] * 513]}
    with pytest.raises(ValueError, match='silent truncation'): ranker('q', hits)

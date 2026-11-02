from types import SimpleNamespace

import pytest

from hybrid_rag.models import answer


def test_real_model_adapter_rejects_invalid_source_numbers(monkeypatch):
    recorded = {}
    content = ['登录会话有效期为20分钟。[1]']
    def create(**kwargs):
        recorded.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content[0]))])
    def client(**kwargs):
        recorded['base_url'] = kwargs['base_url']
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr('openai.OpenAI', client)
    monkeypatch.setenv('ZHIPUAI_API_KEY', 'test-only')
    hit = {'id': 'n1', 'source': 'rules.md', 'heading': '有效期', 'text': '登录会话20分钟。'}
    result = answer('有效期？', [hit], provider='zhipu', model='glm-4.7-flash')
    assert result['provider'] == 'zhipu'
    assert result['citations'][0]['id'] == 'n1'
    assert recorded['model'] == 'glm-4.7-flash'
    assert recorded['base_url'] == 'https://open.bigmodel.cn/api/paas/v4'
    content[0] = '20分钟。[2]'
    with pytest.raises(ValueError, match='source numbers'): answer('有效期？', [hit], provider='zhipu')


def test_no_evidence_does_not_call_model(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-only')
    assert answer('有效期？', [], provider='deepseek')['answer'] == '证据里没有写'

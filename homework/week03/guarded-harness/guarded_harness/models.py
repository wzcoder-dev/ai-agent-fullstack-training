"""Fake Model 只替换模型输出，运行循环仍是实际 smolagents。"""
import os
import time
from smolagents import Model
from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction
from .context_budget import render_context


class ScriptedModel(Model):
    def __init__(self, script, cursor=0):
        super().__init__(model_id='deterministic-fake')
        self.script, self.cursor = script, cursor

    def generate(self, messages, **kwargs):
        if self.cursor >= len(self.script):
            action = {'tool': 'final_answer', 'args': {'answer': '已完成'}}
        else:
            action = self.script[self.cursor]
        self.cursor += 1
        call = ChatMessageToolCall(id=action.get('id', f'call-{self.cursor}'), type='function',
                                  function=ChatMessageToolCallFunction(name=action['tool'], arguments=action['args']))
        return ChatMessage(role='assistant', content='按计划执行下一步', tool_calls=[call])


class MeteredModel(Model):
    def __init__(self, inner, harness):
        super().__init__(model_id=inner.model_id)
        self.inner, self.h = inner, harness

    def generate(self, messages, **kwargs):
        s = self.h.store.data
        self.h.enforce_budget()
        if s['steps'] >= s['max_steps']:
            self.h.stop('MAX_STEPS')
        try:
            messages, stats = render_context(messages, s['context_max_chars'], s['keep_recent'])
        except ValueError:
            self.h.stop('CONTEXT_BUDGET')
        stats['step'] = s['steps'] + 1
        s['context_stats'].append(stats)
        self.h.audit.record('CONTEXT_RENDERED', 'COMPACT' if stats['applied'] else 'KEEP', **stats)
        s['steps'] += 1
        # 在请求模型前保存计数：换进程不会重置预算。
        self.h.checkpoint.save(refresh=False)
        started = time.monotonic()
        self.h.audit.record('MODEL_REQUEST', 'START', model=self.inner.model_id, step=s['steps'])
        kwargs['timeout'] = max(.001, min(45, s['max_duration_seconds'] - s['elapsed_seconds']))
        message = self.inner.generate(messages, **kwargs)
        raw = message.raw
        usage = message.token_usage
        if usage is None and not isinstance(self.inner, ScriptedModel):
            self.h.stop('USAGE_UNKNOWN')
        s['tokens_used'] += (getattr(usage, 'input_tokens', 0) or 0) + (getattr(usage, 'output_tokens', 0) or 0)
        self.h.audit.record('MODEL_RESPONSE', 'DONE', model=self.inner.model_id,
                            response_id=getattr(raw, 'id', None), served_model=getattr(raw, 'model', None),
                            elapsed_seconds=round(time.monotonic() - started, 3),
                            input_tokens=getattr(usage, 'input_tokens', None),
                            output_tokens=getattr(usage, 'output_tokens', None),
                            calls=[{'id': c.id, 'tool': c.function.name} for c in message.tool_calls or []])
        self.h.checkpoint.save(refresh=False)
        self.h.enforce_budget()  # 一次已发生的请求可能超额，但不得再执行工具或生成下一轮。
        return message

    def parse_tool_calls(self, message):
        # 保留底层模型的解析协议，不能悄悄换回基类的文本解析行为。
        return self.inner.parse_tool_calls(message)


def live_model(provider):
    """DeepSeek 使用独立变量，密钥仅传给 SDK，不序列化模型对象。"""
    from smolagents import OpenAIServerModel
    if provider == 'deepseek':
        return OpenAIServerModel(
            model_id=os.environ.get('DEEPSEEK_MODEL', 'deepseek-flash'),
            api_base='https://api.deepseek.com', api_key=os.environ['DEEPSEEK_API_KEY'],
            client_kwargs={'timeout': 45, 'max_retries': 0}, retry=False,
            max_tokens=1500, temperature=0,
            # 当前 DeepSeek 默认 thinking=enabled，和框架的 tool_choice=required 不兼容。
            # 本次明确验证非思考模式的标准工具调用，不保存推理内容。
            extra_body={'thinking': {'type': 'disabled'}})
    if provider != 'openai':
        raise ValueError('未知模型提供商')
    return OpenAIServerModel(model_id=os.environ['MODEL_ID'],
                           api_base=os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1'),
                           api_key=os.environ['OPENAI_API_KEY'],
                           client_kwargs={'timeout': 45, 'max_retries': 0}, retry=False)

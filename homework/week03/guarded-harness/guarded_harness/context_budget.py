"""借鉴课程 3-7：按规则摘要旧步骤；不调用额外模型，不改持久化证据。"""
from __future__ import annotations
import copy
import json
from smolagents.models import ChatMessage
from .state_store import digest


def field(message, key, default=None):
    return message.get(key, default) if isinstance(message, dict) else getattr(message, key, default)


def role_of(message):
    role = field(message, 'role')
    return getattr(role, 'value', role)


def serializable(messages):
    return [dict(role=role_of(m), content=field(m, 'content'),
                 tool_calls=str(field(m, 'tool_calls')) if field(m, 'tool_calls') else None) for m in messages]


def size(messages):
    return len(json.dumps(serializable(messages), ensure_ascii=False, sort_keys=True))


def render_context(messages, max_chars=24000, keep_recent=2):
    """保持 system/任务指令和近期步骤完整；预算装不下这些指令就明确拒绝。"""
    original = copy.deepcopy(messages)
    pinned, groups, active = [], [], []
    for message in original:
        role = role_of(message)
        if role in {'system', 'user'}:
            if active:
                groups.append(active)
                active = []
            pinned.append(message)
        elif role == 'assistant' or (role == 'tool-call' and any(role_of(m) == 'tool-call' for m in active)):
            if active:
                groups.append(active)
            active = [message]
        else:
            active.append(message)
    if active:
        groups.append(active)
    before = size(original)
    if before <= max_chars:
        return original, dict(applied=False, chars_before=before, chars_after=before, summarized_steps=0,
                              kept_steps=len(groups), rendered_hash=digest(serializable(original)))
    # 以整步为单位压缩，不拆 assistant/tool 消息对；较长参数不全文拷入摘要。
    for keep in range(min(keep_recent, len(groups)), -1, -1):
        old = groups[:len(groups)-keep] if keep else groups
        recent = groups[len(groups)-keep:] if keep else []
        summaries = []
        for index, group in enumerate(old, 1):
            text = json.dumps(serializable(group), ensure_ascii=False, sort_keys=True)
            summaries.append(f'步骤{index}摘要（非验收证据）：{text[:160]}… [全文sha256={digest(serializable(group))}]')
        summary = '\n'.join(summaries)
        candidate = pinned + ([ChatMessage(role='user', content='历史步骤仅作摘要；需要细节请重新读取源码：\n'+summary)] if old else []) + [m for g in recent for m in g]
        if size(candidate) > max_chars and old:
            # 摘要本身也需要受控，极长历史只保留确定性的数量与全量指纹。
            candidate = pinned + [ChatMessage(role='user', content=f'已摘要 {len(old)} 个旧步骤。原始事实保留在本地审计。历史sha256={digest(serializable([m for g in old for m in g]))}；需要细节请重新读源码，不凭摘要推断。')] + [m for g in recent for m in g]
        after = size(candidate)
        if after <= max_chars:
            return candidate, dict(applied=True, chars_before=before, chars_after=after,
                                   summarized_steps=len(old), kept_steps=keep,
                                   rendered_hash=digest(serializable(candidate)))
    raise ValueError('CONTEXT_BUDGET：系统与任务指令超过预算，不能安全裁剪')


def recovery_context(state):
    """新进程只带近期结果和旧调用摘要；完整结果仍留在 Checkpoint/Audit。"""
    keep = state['keep_recent']
    history = state['history']
    old = history[:-keep] if keep else history
    recent = history[-keep:] if keep else []
    notes = [dict(id=h['id'], tool=h.get('tool'), ok=h.get('result', {}).get('ok'),
                  result_hash=digest(h.get('result'))) for h in old]
    return dict(plan=state['plan'], steps_used=state['steps'], tokens_used=state['tokens_used'],
                old_steps=notes[-20:], omitted_older_steps=max(0, len(notes)-20), recent_history=recent,
                history_hash=digest(history), evidence=[{k:v for k,v in e.items() if k != 'output'} for e in state['evidence'][-12:]])

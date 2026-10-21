"""Bound transient model requests without changing the saved conversation."""

import copy
import json
import math
import re
import threading
from contextlib import nullcontext
from urllib.parse import urlsplit


_locks = {}
_guard = threading.Lock()


def request_lock(base_url, model, enabled):
    if not enabled:
        return nullcontext()
    url = urlsplit(base_url)
    host = url.hostname
    if host in {'localhost', '127.0.0.1', 'host.docker.internal', '::1'}:
        host = 'local'
    key = (url.scheme, host, url.port, url.path.rstrip('/'), model)
    with _guard:
        return _locks.setdefault(key, threading.RLock())


def estimate_tokens(value):
    # Conservative estimate for mixed Chinese/English JSON. Leave a separate
    # safety margin for the model's chat template; no tokenizer dependency.
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return math.ceil(sum(0.5 if ord(c) < 128 else 2 for c in text))


def fit_messages(messages, tools, input_budget):
    """Keep the system, newest user turn, and complete tool exchanges."""
    messages = copy.deepcopy(messages)
    if input_budget is None:
        return messages
    system = [m for m in messages if m.get('role') == 'system']
    turns = []
    for message in messages:
        if message.get('role') == 'system':
            continue
        if message.get('role') == 'user' or not turns:
            turns.append([])
        turns[-1].append(message)

    def current():
        return system + [message for turn in turns for message in turn]

    def fits():
        return estimate_tokens(current()) + estimate_tokens(tools or []) <= input_budget

    while len(turns) > 1 and not fits():
        turns.pop(0)
    # Large tool results and retrieved memories are expendable excerpts. Tool
    # names/arguments/IDs and the user's current request remain intact.
    for limit in (1200, 600, 250):
        if fits():
            break
        for message in current():
            content = message.get('content')
            if not isinstance(content, str):
                continue
            if message.get('role') == 'tool' and len(content) > limit:
                message['content'] = content[:limit] + '\n[工具结果节选，部分内容省略]'
            elif message.get('role') == 'system':
                message['content'] = re.sub(r'(<memory>)(.*?)(</memory>)',
                    lambda m: m[1] + (m[2] if len(m[2]) <= limit else
                        m[2][:limit] + '\n[记忆节选，完整档案保留在本地]') + m[3],
                    content, flags=re.DOTALL)
    if not fits():
        raise ValueError('当前系统提示、工具和问题超过模型上下文预算；请增大模型上下文或缩短当前请求')
    return current()


def context_overflow(error):
    text = str(error).lower()
    return any(marker in text for marker in (
        'context size has been exceeded', 'context_length_exceeded',
        'maximum context length', 'exceeds the available context',
        'context window', 'context length exceeded', 'prompt is too long'))

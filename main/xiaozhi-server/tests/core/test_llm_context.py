import ast
import copy
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse

from core.providers.llm.request_limits import fit_messages, estimate_tokens, request_lock
from core.providers.llm.cancellation import SessionRequests


def provider_class():
    # Exercise the production provider without importing optional audio/GPU libs.
    path = Path(__file__).resolve().parents[2] / 'core/providers/llm/openai/openai.py'
    node = next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
                if isinstance(n, ast.ClassDef) and n.name == 'LLMProvider')
    from core.providers.llm.request_limits import context_overflow
    scope = dict(LLMProviderBase=object, request_lock=request_lock,
                 fit_messages=fit_messages, context_overflow=context_overflow,
                 logger=Mock(), TAG='test', CompletionUsage=type('Usage', (), {}),
                 urlparse=urlparse, THINKING_DISABLED_DOMAINS={})
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
    return scope['LLMProvider']


class Stream:
    def __init__(self, values):
        self.values, self.closed = values, False

    def __iter__(self):
        for value in self.values:
            if isinstance(value, Exception):
                raise value
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=value, tool_calls=None))])

    def close(self):
        self.closed = True


class ContextTests(unittest.TestCase):
    def provider(self, create):
        provider = provider_class().__new__(provider_class())
        provider.base_url, provider.model_name = 'http://localhost:1234/v1', 'local-test'
        provider._sessions = SessionRequests()
        provider.context_window, provider.serialize_requests = 8192, True
        provider.max_tokens, provider.temperature = 2048, 0.1
        provider.top_p = provider.frequency_penalty = None
        provider._apply_thinking_disabled = lambda params: None
        provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return provider

    def test_history_is_bounded_without_changing_originals_or_breaking_tools(self):
        messages = [{'role':'system','content':'你是小智。'}]
        for _ in range(20):
            messages += [{'role':'user','content':'过去的对话。' * 200},
                         {'role':'assistant','content':'之前的回答。' * 200}]
        messages += [{'role':'user','content':'我最喜欢喝什么？'},
                     {'role':'assistant','tool_calls':[{'id':'call1','function':{'name':'lookup','arguments':'{}'}}]},
                     {'role':'tool','tool_call_id':'call1','content':'结果' * 2000}]
        original = copy.deepcopy(messages)
        trimmed = fit_messages(messages, None, 2500)
        self.assertEqual(original, messages)
        self.assertLessEqual(estimate_tokens(trimmed) + estimate_tokens([]), 2500)
        self.assertEqual('我最喜欢喝什么？', trimmed[1]['content'])
        self.assertEqual('call1', trimmed[2]['tool_calls'][0]['id'])
        self.assertEqual('call1', trimmed[3]['tool_call_id'])

    def test_current_user_and_system_instructions_are_not_silently_cut(self):
        messages = [{'role':'system','content':'系统规则' * 1000}, {'role':'user','content':'当前问题'}]
        with self.assertRaises(ValueError):
            fit_messages(messages, None, 500)

    def test_local_aliases_and_separate_provider_instances_share_a_lock(self):
        self.assertIs(request_lock('http://127.0.0.1:1234/v1', 'same', True),
                      request_lock('http://host.docker.internal:1234/v1', 'same', True))

    def test_session_abort_releases_model_and_does_not_cancel_other_sessions(self):
        entered, closed = threading.Event(), threading.Event()
        class CancellableStream(Stream):
            def __iter__(self):
                entered.set()
                if not closed.wait(2):
                    raise RuntimeError('cancellation did not close HTTP response')
                raise RuntimeError('reader closed by cancellation')
                yield
            def close(self):
                super().close()
                closed.set()
        create = Mock(side_effect=[CancellableStream([]), Stream(['新回答'])])
        provider = self.provider(create)
        with ThreadPoolExecutor(max_workers=1) as executor:
            old = executor.submit(lambda:list(provider.response('old-session', [{'role':'user','content':'旧问题'}])))
            self.assertTrue(entered.wait(1))
            provider.cancel_response('unrelated-session')
            self.assertFalse(closed.is_set())
            provider.cancel_response('old-session')
            self.assertEqual([], old.result(timeout=1))
        self.assertEqual(['新回答'], list(provider.response('new-session', [{'role':'user','content':'新问题'}])))

    def test_overflow_retries_once_before_output_and_closes_both_streams(self):
        first, second = Stream([RuntimeError('Context size has been exceeded.')]), Stream(['恢复成功'])
        create = Mock(side_effect=[first, second])
        provider = self.provider(create)
        self.assertEqual(['恢复成功'], list(provider.response('s', [{'role':'user','content':'你好'}])))
        self.assertEqual(2, create.call_count)
        self.assertLess(create.call_args_list[1].kwargs['max_tokens'], create.call_args_list[0].kwargs['max_tokens'])
        self.assertTrue(first.closed and second.closed)

    def test_overflow_after_partial_output_is_not_replayed(self):
        stream = Stream(['已经说出的内容', RuntimeError('Context size has been exceeded.')])
        create = Mock(return_value=stream)
        response = self.provider(create).response('s', [{'role':'user','content':'你好'}])
        self.assertEqual('已经说出的内容', next(response))
        with self.assertRaises(RuntimeError):
            next(response)
        self.assertEqual(1, create.call_count)
        self.assertTrue(stream.closed)

    def test_lm_studio_qwen_reasoning_setting_reaches_both_request_paths(self):
        provider = self.provider(Mock(side_effect=lambda **kwargs: Stream(['{"ok":true}'])))
        provider.reasoning_effort = 'none'
        del provider._apply_thinking_disabled
        messages = [{'role':'user','content':'返回JSON'}]
        self.assertEqual('{"ok":true}', ''.join(provider.response('s', messages)))
        self.assertEqual([('{"ok":true}', None)], list(provider.response_with_functions('s', messages, functions=[])))
        for call in provider.client.chat.completions.create.call_args_list:
            self.assertEqual('none', call.kwargs['reasoning_effort'])

    def test_structured_extraction_format_is_per_request_and_does_not_change_chat(self):
        from core.memory.engine import EXTRACTION_RESPONSE_FORMAT
        create = Mock(side_effect=lambda **kwargs: Stream(['{"memories":[],"completed_follow_ups":[]}']))
        provider = self.provider(create)
        list(provider.response('', [{'role':'user','content':'提取JSON'}], response_format=EXTRACTION_RESPONSE_FORMAT))
        self.assertEqual('json_schema', create.call_args.kwargs['response_format']['type'])
        list(provider.response('', [{'role':'user','content':'正常聊天'}]))
        self.assertNotIn('response_format', create.call_args.kwargs)

    def test_chat_and_function_streams_cannot_fill_local_kv_cache_concurrently(self):
        started, release, second_started = threading.Event(), threading.Event(), threading.Event()
        class BlockingStream(Stream):
            def __iter__(self):
                started.set()
                if not release.wait(3):
                    raise RuntimeError('test deadlock')
                yield from super().__iter__()
        def first_create(**kwargs):
            return BlockingStream(['第一段'])
        def second_create(**kwargs):
            second_started.set()
            return Stream(['第二段'])
        one, two = self.provider(first_create), self.provider(second_create)
        messages = [{'role':'user','content':'你好'}]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(lambda: list(one.response('1', messages)))
            self.assertTrue(started.wait(1))
            second = pool.submit(lambda: list(two.response_with_functions('2', messages, functions=[])))
            try:
                self.assertFalse(second_started.wait(0.1))
            finally:
                release.set()
            self.assertEqual(['第一段'], first.result(timeout=3))
            self.assertEqual([('第二段', None)], second.result(timeout=3))
        self.assertTrue(second_started.is_set())


if __name__ == '__main__':
    unittest.main()

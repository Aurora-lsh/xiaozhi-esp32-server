"""Actual routing and async inventory refresh, with network/audio replaced."""
import ast
import asyncio
import json
from pathlib import Path
import queue
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from concurrent.futures import Future
import sys

from core.providers.tools.camera_routing import camera_request, dispatch_camera_request, prepare_camera_request
from core.utils.dialogue import Dialogue
from core.providers.tts.dto.dto import SentenceType


def endpoint_module():
    # Avoid optional native Opus dependencies; execute the real client/handler bodies.
    base = Path(__file__).resolve().parents[2] / 'core/providers/tools/mcp_endpoint'
    scope = {'asyncio': asyncio, 'json': json, 'Future': Future, 'logger': Mock(), 'TAG': 'mcp-test',
             'sanitize_tool_name': lambda name: re.sub(r'[^a-zA-Z0-9_-]', '_', name)}
    tree = ast.parse((base/'mcp_endpoint_client.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(base), 'exec'), scope)
    tree = ast.parse((base/'mcp_endpoint_handler.py').read_text(encoding='utf-8'))
    names = {'refresh_mcp_endpoint_tools', '_refresh_notified_tools', 'handle_mcp_endpoint_message'}
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in names:
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(base), 'exec'), scope)
    return SimpleNamespace(**scope)


def tool(name):
    return {'name': name, 'description': name, 'inputSchema': {'type': 'object', 'properties': {}}}


class CameraRoutingTests(unittest.TestCase):
    def test_real_photo_commands_and_wrapped_asr(self):
        for text in ('拍张照看看', '拍张照。', '拍照', '你再拍一张看看前面有什么。', '请用摄像头拍张照片'):
            self.assertEqual('snapshot', camera_request(text)['kind'], text)
        self.assertEqual('snapshot', camera_request(json.dumps({'content': '拍张照看看', 'language': 'zh'}))['kind'])

    def test_colloquial_photo_commands_and_real_asr_message(self):
        for text in ('拍个照看看。', '拍一个照看看', '拍个照片看看', '你再拍个照看看前面有什么。',
                     '帮我拍一个照片', '拍一下照', '照个相看看', '给我照张相', '请照相',
                     '海绵宝宝，拍个照看看。'):
            with self.subTest(text=text):
                request = camera_request(text)
                self.assertIsNotNone(request)
                self.assertEqual('snapshot', request['kind'])
        raw = json.dumps({'content': '拍个照看看。', 'language': 'zh', 'emotion': '😶'}, ensure_ascii=False)
        self.assertEqual({'kind': 'snapshot', 'question': '拍个照看看。'}, camera_request(raw))

    def test_non_camera_actions_do_not_take_photos(self):
        for text in ('拍个篮球', '拍一下手', '帮我拍个电影', '拍个照的故事', '不要拍个照', '我刚才拍个照看看了'):
            self.assertIsNone(camera_request(text), text)

    def test_fiction_history_negation_and_explanations_do_not_open_camera(self):
        for text in ('给我讲一个派大星拍照的故事', '我昨天拍了张照', '不要拍照', '别拍照',
                     '你知道怎么拍照吗', '翻译“拍张照看看”', '假装拍张照', '故事里有一个摄像头'):
            self.assertIsNone(camera_request(text), text)

    def test_capability_question_is_not_a_photo_request(self):
        self.assertEqual('capability', camera_request('你有拍照的功能吗？')['kind'])

    def test_explicit_observation_has_bounded_duration(self):
        self.assertEqual(10, camera_request('连续观察前面20秒')['duration_seconds'])
        self.assertEqual(3, camera_request('观察一下前面')['duration_seconds'])


class InventoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.module = endpoint_module()
        self.conn = SimpleNamespace(func_handler=Mock())
        self.client = self.module.MCPEndpointClient(self.conn)
        await self.client.replace_tools([tool('old_camera')])
        self.sent = []

    def replying_socket(self, pages):
        async def send(message):
            data = json.loads(message)
            self.sent.append(data)
            page = pages[data.get('params', {}).get('cursor', '')]
            asyncio.create_task(self.module.handle_mcp_endpoint_message(self.client,
                json.dumps({'jsonrpc': '2.0', 'id': data['id'], 'result': page})))
        self.client.set_websocket(SimpleNamespace(send=send))

    async def test_complete_refresh_replaces_stale_tools_and_invalidates_both_caches(self):
        self.client.get_available_tools()
        self.replying_socket({'': {'tools': [tool('look_at_camera')], 'nextCursor': 'second'},
                              'second': {'tools': [tool('watch_camera')]}})
        await self.module.refresh_mcp_endpoint_tools(self.client)
        self.assertEqual({'look_at_camera', 'watch_camera'}, set(self.client.tools))
        self.assertEqual(2, len(self.client.get_available_tools()))
        self.assertEqual([3, 4], [item['id'] for item in self.sent])
        self.assertEqual({}, self.client.call_results)
        self.conn.func_handler.tool_manager.refresh_tools.assert_called_once()

    async def test_list_change_notification_does_not_block_its_own_listener(self):
        self.replying_socket({'': {'tools': [tool('look_at_camera')]}})
        await asyncio.wait_for(self.module.handle_mcp_endpoint_message(self.client,
            json.dumps({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})), timeout=0.1)
        await asyncio.wait_for(self.client.tools_refresh_task, timeout=1)
        self.assertIn('look_at_camera', self.client.tools)

    async def test_timeout_keeps_previous_inventory_and_cleans_pending_requests(self):
        self.client.set_websocket(SimpleNamespace(send=AsyncMock()))
        with self.assertRaises(asyncio.TimeoutError):
            await self.module.refresh_mcp_endpoint_tools(self.client, timeout=0.02)
        self.assertEqual({'old_camera'}, set(self.client.tools))
        self.assertFalse(self.client.call_results)

    async def test_repeated_pagination_cursor_fails_without_partial_replacement(self):
        self.replying_socket({'': {'tools': [], 'nextCursor': 'same'},
                              'same': {'tools': [], 'nextCursor': 'same'}})
        with self.assertRaises(RuntimeError):
            await self.module.refresh_mcp_endpoint_tools(self.client)
        self.assertEqual({'old_camera'}, set(self.client.tools))


class PrepareTests(unittest.IsolatedAsyncioTestCase):
    async def test_camera_request_reconnects_a_dead_endpoint_before_refreshing_tools(self):
        old = SimpleNamespace(is_ready=AsyncMock(return_value=False), close=AsyncMock())
        fresh = SimpleNamespace()
        conn = SimpleNamespace(config={'mcp_endpoint':'ws://test.invalid'}, mcp_endpoint_client=old,
                               func_handler=SimpleNamespace(has_tool=lambda name: name == 'look_at_camera'))
        handlers = SimpleNamespace(connect_mcp_endpoint=AsyncMock(return_value=fresh), refresh_mcp_endpoint_tools=AsyncMock())
        with patch.dict(sys.modules, {'core.providers.tools.mcp_endpoint.mcp_endpoint_handler': handlers}):
            plan = await prepare_camera_request(conn, camera_request('拍张照看看'))
        self.assertIs(fresh, conn.mcp_endpoint_client)
        old.close.assert_awaited_once()
        handlers.refresh_mcp_endpoint_tools.assert_awaited_once_with(fresh, timeout=6)
        self.assertEqual('look_at_camera', plan['name'])

    async def test_capability_checks_do_not_take_a_picture_and_use_current_inventory(self):
        client = SimpleNamespace(is_ready=AsyncMock(return_value=True))
        conn = SimpleNamespace(config={}, mcp_endpoint_client=client,
                               func_handler=SimpleNamespace(has_tool=lambda name: False))
        handlers = SimpleNamespace(connect_mcp_endpoint=AsyncMock(), refresh_mcp_endpoint_tools=AsyncMock())
        with patch.dict(sys.modules, {'core.providers.tools.mcp_endpoint.mcp_endpoint_handler': handlers}):
            plan = await prepare_camera_request(conn, camera_request('你有拍照功能吗'))
        self.assertIn('没有接入', plan['response'])
        self.assertNotIn('name', plan)


class CameraResultTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, payload):
        path = Path(__file__).resolve().parents[2]/'core/providers/tools/mcp_endpoint/mcp_endpoint_executor.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name=='execute')
        actions = SimpleNamespace(ERROR='error', REQLLM='model', NOTFOUND='missing')
        scope = {'Dict':dict,'Any':object,'Action':actions,'ActionResponse':lambda **kw: SimpleNamespace(**kw),
                 'call_mcp_endpoint_tool':AsyncMock(return_value=json.dumps(payload))}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), scope)
        conn = SimpleNamespace(mcp_endpoint_client=SimpleNamespace(is_ready=AsyncMock(return_value=True)))
        return await scope['execute'](None, conn, 'look_at_camera', {'question':'拍张照看看'})

    async def test_business_error_is_not_given_to_model_as_an_image_description(self):
        result = await self.execute({'success':False, 'error':'camera offline'})
        self.assertEqual('error', result.action)
        self.assertIn('没有成功返回画面', result.response)

    async def test_successful_scene_remains_available_for_grounded_reply(self):
        result = await self.execute({'success':True, 'description':'桌上有一本书。'})
        self.assertEqual('model', result.action)
        self.assertEqual('桌上有一本书。', json.loads(result.result)['description'])


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    def connection(self):
        return SimpleNamespace(loop=asyncio.get_running_loop(), config={}, sentence_id='turn', client_abort=False,
            logger=Mock(), dialogue=Dialogue(), tts=SimpleNamespace(tts_text_queue=queue.Queue(), store_tts_text=Mock()),
            func_handler=SimpleNamespace(handle_llm_function_call=AsyncMock(return_value=SimpleNamespace(result='画面结果',response=''))),
            _handle_function_result=Mock())

    async def dispatch(self, conn, plan):
        with patch('core.providers.tools.camera_routing.prepare_camera_request', new=AsyncMock(return_value=plan)):
            return await asyncio.to_thread(dispatch_camera_request, conn, camera_request('拍张照看看'), 'turn', Mock())

    async def test_explicit_request_reaches_real_executor_and_records_tool_result(self):
        conn = self.connection()
        self.assertTrue(await self.dispatch(conn, {'name': 'look_at_camera', 'arguments': {'question': '拍张照看看'}}))
        call = conn.func_handler.handle_llm_function_call.call_args.args[1]
        self.assertEqual('look_at_camera', call['name'])
        self.assertEqual({'question': '拍张照看看'}, json.loads(call['arguments']))
        conn._handle_function_result.assert_called_once()
        self.assertEqual(SentenceType.LAST, conn.tts.tts_text_queue.get_nowait().sentence_type)

    async def test_missing_camera_does_not_claim_a_photo_was_taken(self):
        conn = self.connection()
        await self.dispatch(conn, {'response': '摄像头没有接入，这次没有拍到照片。'})
        conn.func_handler.handle_llm_function_call.assert_not_called()
        self.assertIn('没有拍到', conn.dialogue.dialogue[-1].content)

    async def test_failed_capture_is_spoken_as_failure(self):
        conn = self.connection()
        conn.func_handler.handle_llm_function_call.side_effect = RuntimeError('offline')
        await self.dispatch(conn, {'name': 'look_at_camera', 'arguments': {'question': '拍张照看看'}})
        self.assertIn('没有成功返回画面', conn.dialogue.dialogue[-1].content)
        conn._handle_function_result.assert_not_called()

    async def test_late_camera_result_never_enters_replacement_turn(self):
        conn = self.connection()
        async def capture(*args):
            conn.sentence_id = 'replacement'
            return SimpleNamespace(result='旧画面', response='')
        conn.func_handler.handle_llm_function_call.side_effect = capture
        self.assertFalse(await self.dispatch(conn, {'name': 'look_at_camera', 'arguments': {'question': '拍张照看看'}}))
        conn._handle_function_result.assert_not_called()
        self.assertFalse(conn.dialogue.dialogue)
        self.assertTrue(conn.tts.tts_text_queue.empty())

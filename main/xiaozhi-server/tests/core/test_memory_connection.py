"""Run the actual connection methods with audio/network providers replaced.

Loading only these methods avoids importing optional GPU/ASR/native Opus
packages. The production method bodies are executed unchanged.
"""

import ast
import asyncio
import json
import queue
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from typing import Any, Dict
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from core.utils.dialogue import Dialogue, Message
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO


def connection_class():
    path = Path(__file__).resolve().parents[2] / "core/connection.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ConnectionHandler")
    names = {"_initialize_memory", "chat", "_route_message", "change_system_prompt"}
    methods = [node for node in original.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    scope = {"asyncio": asyncio, "uuid": uuid, "json": json, "Message": Message,
             "ContentType": ContentType, "SentenceType": SentenceType, "TTSMessageDTO": TTSMessageDTO,
             "TAG": "memory-integration", "handleTextMessage": AsyncMock()}
    for method in methods:
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), scope)
    return type("ConnectionTextHarness", (), {name: scope[name] for name in names})


class Model:
    def __init__(self):
        self.received = []
        self.command = {"action": "none"}

    def response_no_stream(self, system, prompt, **kwargs):
        return json.dumps(self.command, ensure_ascii=False)

    def response(self, session, messages):
        self.received = messages
        yield "收到。"


class MemoryConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.harness = connection_class()
        self.connections = []

    async def asyncTearDown(self):
        self.temp.cleanup()

    def connection(self, device, character=False):
        conn = self.harness()
        conn.config = {"local_memory": {"enabled": True, "database_path": str(Path(self.temp.name) / "memory.db"),
                                         "migrate_legacy": False, "query_rewrite": False}}
        if character:
            conn.config["local_memory"]["character"] = {"enabled": True}
        conn.device_id = device
        conn.session_id = str(uuid.uuid4())
        conn.llm = None
        conn.memory = None
        conn.read_config_from_api = True
        conn.dialogue = Dialogue()
        conn.dialogue.update_system_message("你是小智。")
        conn._initialize_memory()
        conn.memory.schedule_extraction = lambda: None
        conn.llm = Model()
        conn.memory.set_llm(conn.llm)
        conn.tts = Mock()
        conn.tts.tts_text_queue = queue.Queue()
        conn.logger = Mock()
        conn.intent_type = "nointent"
        conn.features = {"emoji": False}
        conn.current_speaker = None
        conn.system_introduced_speakers = set()
        conn.client_abort = False
        conn.loop = asyncio.get_running_loop()
        self.connections.append(conn)
        return conn

    async def test_character_prompt_and_story_hook_reach_real_chat_path(self):
        conn = self.connection("one", character=True)
        conn.change_system_prompt("请用中文回答。")
        story = "我和派大星在水母田做了一辆纸风车，带回比奇堡的菠萝屋，放在窗边。小蜗对着风车转了一圈，我们说好明天再带出去。"
        conn.llm.response = lambda *args: iter([story])
        await asyncio.to_thread(conn.chat, "讲一个你和派大星的新故事")
        book = conn.memory.character
        self.assertEqual(4, book.list()["total"])
        identity = book.focus
        book.edit(identity, {"content": "我和派大星把纸风车改放在厨房的书架上。"})
        conn.llm.response = Model.response.__get__(conn.llm)
        await asyncio.to_thread(conn.chat, "继续上次你和派大星的纸风车故事")
        sent = json.dumps(conn.llm.received, ensure_ascii=False)
        self.assertIn("你在角色扮演中是海绵宝宝", sent)
        self.assertIn("书架", sent)
        self.assertNotIn("放在窗边", sent)
        self.assertEqual(0, conn.memory.store.list_memories("one")["total"])

    async def test_connections_do_not_rebind_each_others_device_or_archive_hooks(self):
        one, two = self.connection("one"), self.connection("two")
        await asyncio.to_thread(one.chat, "我的猫叫奶茶")
        await asyncio.to_thread(two.chat, "我的猫叫布丁")
        self.assertIsNot(one.memory, two.memory)
        self.assertEqual("one", one.memory.role_id)
        self.assertEqual("two", two.memory.role_id)
        self.assertEqual(2, one.memory.store.list_messages("one")["total"])
        self.assertEqual(2, two.memory.store.list_messages("two")["total"])
        self.assertNotIn("布丁", one.memory.store.list_messages("one")["items"][-1]["content"])

    async def test_saved_memory_is_supplied_to_chat_model_without_prompt_placeholder(self):
        conn = self.connection("one")
        source = Message("user", "我的猫叫奶茶")
        conn.memory.record_message(source)
        conn.memory.store.remember("one", source.uniq_id, "用户的猫叫奶茶")
        await asyncio.to_thread(conn.chat, "我的猫叫什么？")
        self.assertIn("用户的猫叫奶茶", conn.llm.received[0]["content"])
        self.assertEqual("收到。", conn.memory.store.list_messages("one")["items"][0]["content"])

    async def test_old_stream_closes_and_never_archives_into_a_new_turn(self):
        conn = self.connection('one')
        closed = []
        def replaced_stream(*args):
            try:
                conn.sentence_id = 'replacement-turn'
                yield '旧回答不应进入新会话。'
            finally:
                closed.append(True)
        conn.llm.response = replaced_stream
        self.assertFalse(await asyncio.to_thread(conn.chat, '测试中途换一轮'))
        self.assertEqual([True], closed)
        self.assertEqual(1, conn.memory.store.list_messages('one')['total'])

    async def test_pure_character_story_avoids_loading_device_tool_schemas(self):
        conn = self.connection('one', character=True)
        conn.intent_type = 'function_call'
        conn.func_handler = Mock()
        await asyncio.to_thread(conn.chat, '给我讲一个你和派大星的新日常')
        conn.func_handler.get_functions.assert_not_called()
        self.assertTrue(conn.llm.received)

    async def test_web_correction_clears_stale_conversation_before_next_model_request(self):
        conn = self.connection("one")
        source = Message("user", "我的猫叫奶茶")
        conn.dialogue.put(source)
        memory = conn.memory.store.remember("one", source.uniq_id, "用户的猫叫奶茶")
        conn.memory.consume_context_reset()
        conn.dialogue.put(Message("assistant", "你的猫叫奶茶。"))
        conn.memory.store.edit("one", memory["id"], {"content": "用户的猫叫布丁"})
        await asyncio.to_thread(conn.chat, "我的猫叫什么？")
        sent = json.dumps(conn.llm.received, ensure_ascii=False)
        self.assertIn("布丁", sent)
        self.assertNotIn("奶茶", sent)

    async def test_explicit_voice_delete_works_without_function_calling_and_finishes_tts(self):
        conn = self.connection("one")
        source = Message("user", "我的猫叫奶茶")
        conn.memory.record_message(source)
        memory = conn.memory.store.remember("one", source.uniq_id, "用户的猫叫奶茶")
        conn.llm.command = {"action": "delete", "ids": [memory["id"]]}
        await asyncio.to_thread(conn.chat, "忘掉我的猫叫什么")
        self.assertEqual(0, conn.memory.store.list_memories("one")["total"])
        self.assertEqual([], conn.llm.received)
        stages = [conn.tts.tts_text_queue.get_nowait().sentence_type for _ in range(conn.tts.tts_text_queue.qsize())]
        self.assertEqual([SentenceType.FIRST, SentenceType.MIDDLE, SentenceType.LAST], stages)

    async def test_first_hello_survives_slow_private_config_initialization(self):
        conn = self.harness()
        conn.bind_completed_event = asyncio.Event()
        conn.need_bind = False
        handler = conn._route_message.__globals__["handleTextMessage"]
        hello = json.dumps({"type": "hello"})
        task = asyncio.create_task(conn._route_message(hello))
        await asyncio.sleep(1.1)
        self.assertFalse(task.done(), "The initial hello must not be discarded after one second")
        handler.assert_not_awaited()
        conn.bind_completed_event.set()
        await task
        handler.assert_awaited_once_with(conn, hello)

    async def test_unknown_binding_does_not_try_to_play_a_prompt_without_tts(self):
        conn = self.harness()
        conn.bind_completed_event = asyncio.Event()
        conn.tts = None
        conn._discard_message_with_bind_prompt = AsyncMock()
        await conn._route_message(b"early audio")
        conn._discard_message_with_bind_prompt.assert_not_awaited()

    async def test_confirmed_unbound_device_still_gets_binding_prompt(self):
        conn = self.harness()
        conn.bind_completed_event = asyncio.Event()
        conn.bind_completed_event.set()
        conn.need_bind = True
        conn._discard_message_with_bind_prompt = AsyncMock()
        handler = conn._route_message.__globals__["handleTextMessage"]
        await conn._route_message(json.dumps({"type": "listen"}))
        conn._discard_message_with_bind_prompt.assert_awaited_once()
        handler.assert_not_awaited()


class MemoryConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_api_keeps_host_database_and_enable_switch(self):
        path = Path(__file__).resolve().parents[2] / "config/config_loader.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {"get_config_from_api_async", "merge_configs"}
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names]
        async def server_config():
            return {"server": {"auth": {"enabled": True}}, "local_memory": {"database_path": "remote.db"}}
        from collections.abc import Mapping
        scope = {"init_service": Mock(), "get_server_config": server_config, "Mapping": Mapping,
                 "get_project_dir": lambda: "", "read_config": lambda _: {
                     "local_memory": {"enabled": True, "database_path": "data/default.db", "query_rewrite": True}}}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
        for enabled in (True, False):
            result = await scope["get_config_from_api_async"]({"manager-api": {}, "local_memory": {
                "enabled": enabled, "database_path": "data/owner.db"}})
            self.assertEqual(enabled, result["local_memory"]["enabled"])
            self.assertEqual("data/owner.db", result["local_memory"]["database_path"])
            self.assertTrue(result["local_memory"]["query_rewrite"])

    async def test_local_mode_does_not_initialize_selected_cloud_memory_service(self):
        path = Path(__file__).resolve().parents[2] / "core/utils/modules_initialize.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "initialize_modules")
        create = Mock(return_value="local-provider")
        scope = {"Dict": Dict, "Any": Any, "memory": SimpleNamespace(create_instance=create), "TAG": "test"}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), scope)
        result = scope["initialize_modules"](Mock(), {"selected_module": {"Memory": "cloud"},
            "local_memory": {"enabled": True, "database_path": "local.db"}}, init_memory=True)
        self.assertEqual("local-provider", result["memory"])
        self.assertEqual("mem_local_long", create.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

"""Voice teaching persists complete recipes; execution uses real controller code."""
import ast
import asyncio
import json
import queue
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.providers.tools.action_teaching import (
    ActionLibrary, action_teaching_request, action_name, describe_steps, parse_steps,
    dispatch_action_command, replay_action,
)
from core.providers.tools.medicine_navigation import stop_navigation
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Dialogue, Message
with patch("config.logger.setup_logging", return_value=Mock()):
    from plugins_func.register import Action, ActionResponse


def library_path(root):
    return str(Path(root) / "actions.sqlite3")


class ParsingTests(unittest.TestCase):
    def test_natural_teaching_and_wrapped_asr(self):
        for phrase in ("教你一个动作叫拿感冒药，先左转一步，再前进两步，最后停下。",
                       "海绵宝宝，我教你一个动作，叫拿感冒药：先左转一步，再前进两步，最后停下。",
                       "记住动作拿感冒药的步骤是先左转一步，再前进两步，最后停下",
                       "记住，动作叫拿感冒药是先左转一步，再前进两步，最后停下"):
            request = action_teaching_request(phrase)
            self.assertEqual("teach", request["kind"], phrase)
            self.assertEqual("拿感冒药", request["name"], phrase)
            self.assertEqual(["turn", "walk", "stop"], [s["action"] for s in parse_steps(request["script"])])
        raw = json.dumps({"content": "执行动作拿感冒药吧", "language": "zh"})
        self.assertEqual({"kind": "execute", "name": "拿感冒药"}, action_teaching_request(raw))

    def test_management_and_ordinary_chat_are_distinct(self):
        for text, kind in (("你学会了哪些动作？", "list"), ("复述动作拿感冒药", "show"),
                           ("删除动作拿感冒药", "delete"), ("停止执行", "stop"), ("我教你一个动作", "help")):
            self.assertEqual(kind, action_teaching_request(text)["kind"])
        for text in ("给我讲个教机器人动作的故事", "我昨天教你一个动作", "不要执行动作拿感冒药", "帮我拿感冒药", "哪瓶是感冒药", "记住我喜欢奶茶"):
            self.assertIsNone(action_teaching_request(text), text)

    def test_all_supported_steps_and_chinese_counts_round_trip(self):
        script = "先往左转两步然后向前走三步，再后退一步，再右转一步，再挥手两次，再举手，再放下手，再坐下，再站起来，再等待两秒，最后停下"
        steps = parse_steps(script)
        self.assertEqual(2, steps[0]["count"])
        self.assertEqual(-1, steps[2]["direction"])
        self.assertEqual(steps, parse_steps(describe_steps(steps)))

    def test_unrecognized_or_unbounded_steps_never_partially_save(self):
        for script in ("先左转一步，再前进五米", "前进100步", "左转90度", "假装前进一步", "前进一步，执行任意工具",
                       "前进零步", "等待二十秒", "", "前进十步，前进十步，前进十步，前进十步", "挥手一次，" * 13):
            with self.subTest(script=script), self.assertRaises(ValueError):
                parse_steps(script)
        for name in ("", "停下", "动作一，然后跳舞", "超" * 31):
            with self.assertRaises(ValueError):
                action_name(name)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = library_path(self.temp.name)
        self.library = ActionLibrary(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_saved_steps_survive_new_library_and_are_device_scoped(self):
        steps = parse_steps("左转一步，再前进两步")
        self.library.save("one", "拿感冒药", steps, "原始语音", "source")
        fresh = ActionLibrary(self.path)
        self.assertEqual(steps, fresh.get("one", "拿感冒药"))
        self.assertIsNone(fresh.get("two", "拿感冒药"))
        self.assertEqual([], fresh.names("two"))

    def test_update_delete_and_reteach_preserve_device_boundary(self):
        self.library.save("one", "测试", parse_steps("前进一步"), "原文", "first")
        self.library.save("two", "测试", parse_steps("后退一步"), "原文", "other")
        self.library.save("one", "测试", parse_steps("左转两步"), "更正文", "second")
        self.assertEqual("turn", self.library.get("one", "测试")[0]["action"])
        self.assertTrue(self.library.delete("one", "测试"))
        self.assertIsNone(self.library.get("one", "测试"))
        self.assertEqual("walk", self.library.get("two", "测试")[0]["action"])
        self.library.save("one", "测试", parse_steps("站起来"), "重教", "third")
        self.assertEqual(["测试"], self.library.names("one"))

    def test_corrupted_record_cannot_inject_tools(self):
        self.library.save("one", "测试", parse_steps("前进一步"), "原文", "source")
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE learned_actions SET steps_json=?", (json.dumps([{"action": "arbitrary_tool", "count": 1, "direction": 1}]),))
        with self.assertRaises((ValueError, KeyError)):
            self.library.get("one", "测试")


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        self.temp.cleanup()

    def connection(self, device="one"):
        calls = []
        async def execute(conn, call):
            calls.append((call["name"], json.loads(call["arguments"])))
            value = "idle" if call["name"] == "self_otto_get_status" else "true"
            return ActionResponse(Action.REQLLM, result=value)
        return SimpleNamespace(device_id=device, loop=asyncio.get_running_loop(), sentence_id="turn", client_abort=False,
            config={"local_memory": {"database_path": library_path(self.temp.name)}}, calls=calls,
            logger=Mock(), memory=None, dialogue=Dialogue(), intent_type="function_call",
            func_handler=SimpleNamespace(has_tool=lambda name: True, handle_llm_function_call=AsyncMock(side_effect=execute)),
            tts=SimpleNamespace(tts_text_queue=queue.Queue(), store_tts_text=Mock()))

    async def command(self, conn, text):
        with patch("core.providers.tools.action_teaching.asyncio.sleep", new=AsyncMock()):
            return await asyncio.to_thread(dispatch_action_command, conn, action_teaching_request(text), "turn", Message("user", text), Mock())

    async def test_teach_only_saves_and_reads_back_without_motion_or_llm(self):
        conn = self.connection()
        await self.command(conn, "教你一个动作叫拿感冒药：先左转一步，再前进两步，最后停下")
        self.assertFalse(conn.calls)
        response = conn.dialogue.dialogue[-1].content
        self.assertIn("已保存", response)
        self.assertIn("左转1步，再前进2步，再停下", response)
        self.assertEqual(SentenceType.MIDDLE, conn.tts.tts_text_queue.get_nowait().sentence_type)
        self.assertEqual(SentenceType.LAST, conn.tts.tts_text_queue.get_nowait().sentence_type)
        fresh = self.connection()
        await self.command(fresh, "复述动作拿感冒药")
        self.assertIn("前进2步", fresh.dialogue.dialogue[-1].content)
        self.assertFalse(fresh.calls)

    async def test_named_execution_waits_after_each_single_step(self):
        conn = self.connection()
        await self.command(conn, "教你一个动作叫拿感冒药：左转一步，再前进两步，停下")
        await self.command(conn, "执行拿感冒药")
        actions = [args for name, args in conn.calls if name == "self_otto_action"]
        self.assertEqual(["turn", "walk", "walk"], [args["action"] for args in actions])
        self.assertTrue(all(a["steps"] == 1 and a["arm_swing"] == 0 for a in actions))
        names = [name for name, _ in conn.calls]
        for i, name in enumerate(names):
            if name == "self_otto_action":
                self.assertEqual("self_otto_get_status", names[i + 1])
        self.assertEqual("self_otto_stop", names[-1])
        self.assertIn("执行完毕", conn.dialogue.dialogue[-1].content)

    async def test_unknown_name_or_invalid_teaching_never_moves(self):
        conn = self.connection()
        await self.command(conn, "执行未学动作")
        self.assertIn("还没有学过", conn.dialogue.dialogue[-1].content)
        await self.command(conn, "教你一个动作叫错误路线：左转一步，再前进五米")
        self.assertFalse(conn.calls)
        self.assertIsNone(ActionLibrary(library_path(self.temp.name)).get("one", "错误路线"))

    async def test_error_acknowledgement_stops_remaining_steps(self):
        conn = self.connection()
        original = conn.func_handler.handle_llm_function_call.side_effect
        async def fail(c, call):
            if call["name"] == "self_otto_action":
                conn.calls.append((call["name"], json.loads(call["arguments"])))
                return ActionResponse(Action.REQLLM, result="错误：此动作需要手部舵机支持")
            return await original(c, call)
        conn.func_handler.handle_llm_function_call.side_effect = fail
        with patch("core.providers.tools.action_teaching.asyncio.sleep", new=AsyncMock()):
            answer = await replay_action(conn, "挥手后走", parse_steps("挥手，再前进两步"), "turn", Mock())
        self.assertNotIn("执行完毕", answer)
        self.assertEqual(1, sum(name == "self_otto_action" for name, _ in conn.calls))
        self.assertEqual("self_otto_stop", conn.calls[-1][0])

    async def test_existing_abort_handler_cancels_taught_action(self):
        conn = self.connection()
        started = asyncio.Event()
        original = conn.func_handler.handle_llm_function_call.side_effect
        async def block(c, call):
            if call["name"] == "self_otto_action":
                started.set()
                await asyncio.Event().wait()
            return await original(c, call)
        conn.func_handler.handle_llm_function_call.side_effect = block
        with patch("core.providers.tools.action_teaching.asyncio.sleep", new=AsyncMock()):
            task = asyncio.create_task(replay_action(conn, "路线", parse_steps("前进三步"), "turn", Mock()))
            await asyncio.wait_for(started.wait(), 1)
            await stop_navigation(conn)
        self.assertTrue(task.cancelled())
        self.assertEqual("self_otto_stop", conn.calls[-1][0])
        self.assertIsNone(conn._medicine_navigation)

    async def test_stale_generation_never_runs_another_step(self):
        conn = self.connection()
        original = conn.func_handler.handle_llm_function_call.side_effect
        async def stale(c, call):
            value = await original(c, call)
            if call["name"] == "self_otto_action":
                conn.sentence_id = "replacement"
            return value
        conn.func_handler.handle_llm_function_call.side_effect = stale
        with patch("core.providers.tools.action_teaching.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await replay_action(conn, "路线", parse_steps("前进两步"), "turn", Mock())
        self.assertEqual(1, sum(name == "self_otto_action" for name, _ in conn.calls))
        self.assertEqual("self_otto_stop", conn.calls[-1][0])

    async def test_failed_stop_overrides_completed_sequence(self):
        conn = self.connection()
        original = conn.func_handler.handle_llm_function_call.side_effect
        async def failed_stop(c, call):
            if call["name"] == "self_otto_stop" and len(conn.calls) > 1:
                return ActionResponse(Action.ERROR, response="offline")
            return await original(c, call)
        conn.func_handler.handle_llm_function_call.side_effect = failed_stop
        with patch("core.providers.tools.action_teaching.asyncio.sleep", new=AsyncMock()):
            answer = await replay_action(conn, "路线", parse_steps("前进一步"), "turn", Mock())
        self.assertIn("没有确认停止", answer)
        self.assertNotIn("执行完毕", answer)

    async def test_missing_tools_prevent_all_motion(self):
        conn = self.connection()
        conn.func_handler.has_tool = lambda name: False
        answer = await replay_action(conn, "路线", parse_steps("前进一步"), "turn", Mock())
        self.assertIn("尚未接通", answer)
        self.assertFalse(conn.calls)

    async def test_stalled_motion_status_stops_instead_of_queueing_next_step(self):
        conn = self.connection()
        original = conn.func_handler.handle_llm_function_call.side_effect
        async def moving(c, call):
            value = await original(c, call)
            if call["name"] == "self_otto_get_status":
                return ActionResponse(Action.REQLLM, result="moving")
            return value
        conn.func_handler.handle_llm_function_call.side_effect = moving
        with patch("core.providers.tools.action_teaching.asyncio.sleep", new=AsyncMock()):
            answer = await replay_action(conn, "路线", parse_steps("前进三步"), "turn", Mock())
        self.assertNotIn("执行完毕", answer)
        self.assertEqual(0, sum(name == "self_otto_action" for name, _ in conn.calls))
        self.assertEqual("self_otto_stop", conn.calls[-1][0])

    async def test_asr_teaching_bypasses_unrelated_intent_model(self):
        path = Path(__file__).resolve().parents[2] / "core/handle/receiveAudioHandle.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        method = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "startToChat")
        intent, stt = AsyncMock(), AsyncMock()
        scope = {"ConnectionHandler": object, "json": json, "TAG": "test", "Message": Message,
            "handle_user_intent": intent, "send_stt_message": stt}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), scope)
        conn = self.connection()
        conn.need_bind, conn.max_output_size, conn.client_is_speaking = False, 0, False
        conn.executor, conn.chat = Mock(), Mock()
        conn.intent_type = "intent_llm"
        raw = json.dumps({"content": "教你一个动作叫拿感冒药，左转一步，再前进两步", "language": "zh"})
        await scope["startToChat"](conn, raw)
        intent.assert_not_called()
        stt.assert_awaited_once_with(conn, raw)
        conn.executor.submit.assert_called_once_with(conn.chat, raw)

    async def test_real_chat_routes_teaching_before_generic_memory(self):
        path = Path(__file__).resolve().parents[2] / "core/connection.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ConnectionHandler")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "chat")
        scope = dict(Message=Message, uuid=uuid, TTSMessageDTO=TTSMessageDTO, SentenceType=SentenceType,
            ContentType=ContentType, TAG="test", enqueue_tool_report=Mock())
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), scope)
        conn = self.connection()
        conn.memory = SimpleNamespace(handle_command=Mock(side_effect=AssertionError("must route teaching first")))
        self.assertTrue(await asyncio.to_thread(scope["chat"], conn, "记住动作拿感冒药的步骤是左转一步，再前进两步"))
        self.assertFalse(conn.calls)
        conn.memory.handle_command.assert_not_called()
        self.assertIn("已保存", conn.dialogue.dialogue[-1].content)


if __name__ == "__main__":
    unittest.main()

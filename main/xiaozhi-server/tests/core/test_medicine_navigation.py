"""Exercise navigation decisions with actual MCP result shapes and fake hardware."""
import ast
import asyncio
import importlib.util
import json
import queue
import uuid
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

from core.providers.tools.medicine_navigation import (
    medicine_request, navigation_prompt, observation, navigate, stop_navigation,
    dispatch_medicine_request,
    medicine_identification_request, identification_prompt, identification_position,
)
from core.utils.dialogue import Dialogue, Message
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
with patch("config.logger.setup_logging", return_value=Mock()):
    from plugins_func.register import Action, ActionResponse


class RequestTests(unittest.TestCase):
    options = {"identity_mode": "marker", "targets": {"感冒药": "蓝色易拉罐", "发烧药": "绿色易拉罐", "头痛药": "橙色易拉罐"}}

    def test_real_scene_question_and_short_variants_identify_without_movement(self):
        for text in ("海绵宝宝，你面前有三瓶药，哪一瓶是感冒药？", "哪罐是感冒药", "感冒药在哪边", "看看哪个是感冒药", "请识别感冒药"):
            with self.subTest(text=text):
                self.assertEqual({"target": "感冒药", "identify": True, "observe": True},
                    medicine_identification_request(text, self.options))
        text = json.dumps({"content": "哪一瓶是感冒药？", "language": "zh"})
        self.assertTrue(medicine_identification_request(text, self.options)["identify"])

    def test_mapping_question_uses_configuration_without_claiming_to_see_it(self):
        self.assertEqual({"target": "感冒药", "identify": True, "observe": False},
            medicine_identification_request("感冒药是什么颜色？", self.options))

    def test_unrelated_or_quoted_medicine_question_is_not_a_scene_request(self):
        for text in ("感冒药的副作用是什么", "不要识别感冒药", "讲个感冒药是什么颜色的故事", "翻译哪瓶是感冒药", "我昨天问了哪瓶是感冒药", "感冒药", "感冒药怎么服用", "哪瓶是感冒药哪瓶是发烧药"):
            self.assertIsNone(medicine_identification_request(text, self.options), text)
    def test_commands_and_wrapped_asr(self):
        for text in ("帮我拿感冒药", "请帮我拿一下感冒药。", "海绵宝宝，帮我找感冒药", "走到感冒药的面前", "去感冒药旁边"):
            with self.subTest(text=text):
                self.assertEqual({"target": "感冒药"}, medicine_request(text))
        self.assertEqual({"target": "布洛芬"}, medicine_request(json.dumps({"content": "帮我拿一盒布洛芬"})))

    def test_questions_negation_fiction_and_fragments_do_not_move(self):
        for text in ("感冒药的面前。", "感冒药是什么颜色", "拿什么药", "不要帮我拿感冒药", "帮我拿感冒药的故事", "翻译帮我拿感冒药", "我昨天拿了感冒药", "怎么找感冒药", "帮我拿水", "帮我拿感冒药然后跳舞"):
            self.assertIsNone(medicine_request(text), text)

    def test_explicit_stop(self):
        for text in ("停下", "别走了", "停止寻药", "海绵宝宝，停止！"):
            self.assertEqual({"stop": True}, medicine_request(text))

    def test_protocol_fits_firmware_without_truncation(self):
        for target in ("感冒药", "布洛芬", "蓝色瓶子"):
            prompt = navigation_prompt(target)
            self.assertTrue(prompt.startswith("NAV1 Find " + target))
            self.assertLessEqual(len(quote(prompt[5:], safe="")), 255)
        with self.assertRaises(ValueError):
            navigation_prompt("非常长的药品名字" * 10)
        prompt = identification_prompt("蓝色易拉罐")
        self.assertLessEqual(len(quote(prompt[5:], safe="")), 255)


def photo(data):
    return json.dumps({"success": True, "description": json.dumps(data)})


class ObservationTests(unittest.TestCase):
    def test_actual_live_object_result_is_accepted_without_changing_coordinates(self):
        payload = {"success": True, "description": '{"match":1,"x":0.513}'}
        self.assertEqual("中间", identification_position(payload))
        for data in ({"match": 1}, {"x": .513}, {"match": None, "x": .513},
                     {"match": 1, "x": "0.513"}, {"match": 1, "x": 2}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                identification_position(photo(data))

    def test_named_navigation_fields_keep_all_motion_guards(self):
        self.assertEqual({"match": True, "x": .2, "height": .3, "clear": True},
            observation(photo({"match": 1, "x": .2, "h": .3, "clear": 1})))
        for data in ({"match": 1, "x": .2, "h": .3},
                     {"match": 1, "x": .2, "h": None, "clear": 1},
                     {"match": 1, "x": .2, "h": .3, "clear": None}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                observation(photo(data))

    def test_identification_needs_no_ground_or_motion_observation(self):
        self.assertEqual("左侧", identification_position(photo([1, .2])))
        self.assertEqual("中间", identification_position(photo([1, .5])))
        self.assertEqual("右侧", identification_position(photo([1, .8])))
        self.assertIsNone(identification_position(photo([0, None])))
        for frame in ([None, None], [1, None], [1, 2], [True, .2]):
            with self.assertRaises(ValueError):
                identification_position(photo(frame))
    def test_coordinates_and_absent_target(self):
        self.assertEqual(0.2, observation(photo([1, .2, .3, 1]))["x"])
        self.assertFalse(observation(photo([0, None, None, 1]))["match"])

    def test_uncertainty_prose_and_invalid_coordinates_cannot_move(self):
        for payload in (photo([1, None, .2, 1]), photo([1, .5, 0, 1]),
                        photo([1, -.1, .3, 1]), photo([1, .5, float('nan'), 1]),
                        photo([1, .5, .3, None]), photo([True, .5, .3, 1]),
                        photo([1, .5, .3]), '{"success":false}',
                        '{"success":true,"description":"蓝瓶在左边"}'):
            with self.subTest(payload=payload), self.assertRaises((ValueError, TypeError)):
                observation(payload)


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    def connection(self, frames, options=None):
        calls = []
        images = iter(frames)

        async def execute(conn, data):
            name = data["name"]
            args = json.loads(data["arguments"])
            calls.append((name, args))
            if name == "look_at_camera":
                value = photo(next(images))
            elif name == "self_otto_get_status":
                value = "idle"
            else:
                value = "true"
            return ActionResponse(Action.REQLLM, result=value)

        return SimpleNamespace(sentence_id="turn", client_abort=False, calls=calls,
            config={"medicine_navigation": options or {}}, dialogue=Dialogue(), logger=Mock(),
            func_handler=SimpleNamespace(has_tool=lambda name: True, tool_manager=Mock(),
                                         handle_llm_function_call=AsyncMock(side_effect=execute)))

    async def run_navigation(self, conn, target="感冒药"):
        with patch("core.providers.tools.camera_routing.prepare_camera_request", new=AsyncMock()), \
             patch("core.providers.tools.medicine_navigation.asyncio.sleep", new=AsyncMock()):
            return await navigate(conn, {"target": target}, "turn", Mock())

    def movements(self, conn):
        return [args for name, args in conn.calls if name == "self_otto_action"]

    async def identify(self, conn, observe=True):
        with patch("core.providers.tools.camera_routing.prepare_camera_request", new=AsyncMock()):
            return await navigate(conn, {"target": "感冒药", "identify": True, "observe": observe}, "turn", Mock())

    async def test_identification_uses_only_camera_and_accepts_demo_cans(self):
        conn = self.connection([[1, .8]], {"identity_mode": "marker", "targets": {"感冒药": "蓝色易拉罐"}})
        conn.func_handler.has_tool = lambda name: name == "look_at_camera"
        answer = await self.identify(conn)
        self.assertEqual("蓝色易拉罐代表感冒药，我在画面右侧看到了它。", answer)
        self.assertEqual(["look_at_camera"], [name for name, _ in conn.calls])
        self.assertIn("Find 蓝色易拉罐", conn.calls[0][1]["question"])

    async def test_actual_live_response_reaches_spoken_identification(self):
        conn = self.connection([{"match": 1, "x": .513}],
            {"identity_mode": "marker", "targets": {"感冒药": "蓝色易拉罐"}})
        answer = await self.identify(conn)
        self.assertEqual("蓝色易拉罐代表感冒药，我在画面中间看到了它。", answer)
        self.assertEqual(["look_at_camera"], [name for name, _ in conn.calls])

    async def test_mapping_reply_never_captures_or_claims_a_current_position(self):
        conn = self.connection([], {"identity_mode": "marker", "targets": {"感冒药": "蓝色易拉罐"}})
        self.assertEqual("按我们的演示约定，蓝色易拉罐代表感冒药。", await self.identify(conn, observe=False))
        self.assertFalse(conn.calls)

    async def test_absent_or_uncertain_can_never_uses_a_position_from_memory(self):
        for view in ([0, None], [None, None], [1, None]):
            conn = self.connection([view], {"identity_mode": "marker", "targets": {"感冒药": "蓝色易拉罐"}})
            answer = await self.identify(conn)
            self.assertNotIn("看到了", answer)
            self.assertEqual(["look_at_camera"], [name for name, _ in conn.calls])

    async def test_identification_still_works_when_movement_is_disabled(self):
        conn = self.connection([[1, .5]], {"enabled": False, "identity_mode": "marker", "targets": {"感冒药": "蓝色易拉罐"}})
        self.assertIn("画面中间", await self.identify(conn))
        self.assertFalse(self.movements(conn))

    async def test_left_turn_forward_then_two_fresh_near_frames(self):
        conn = self.connection([[1, .2, .15, 1], [1, .5, .2, 1], [1, .5, .5, 1], [1, .5, .5, 1]])
        answer = await self.run_navigation(conn)
        self.assertEqual(["turn", "walk"], [a["action"] for a in self.movements(conn)])
        self.assertTrue(all(a["steps"] == 1 and a["arm_swing"] == 0 for a in self.movements(conn)))
        self.assertIn("就在前方", answer)
        self.assertEqual("self_otto_stop", conn.calls[-1][0])
        # Each movement is followed by a completed-status check before capture.
        names = [name for name, _ in conn.calls]
        for index, name in enumerate(names):
            if name == "self_otto_action":
                self.assertEqual("self_otto_get_status", names[index + 1])
        self.assertIsNone(conn._medicine_navigation)

    async def test_right_turn_uses_negative_direction(self):
        conn = self.connection([[1, .9, .2, 1], [1, .5, .5, 1], [1, .5, .5, 1]])
        await self.run_navigation(conn)
        self.assertEqual(-1, self.movements(conn)[0]["direction"])

    async def test_search_rotates_only_and_is_bounded(self):
        conn = self.connection([[0, None, None, 1]] * 6)
        self.assertIn("没有找到", await self.run_navigation(conn))
        self.assertEqual(["turn"] * 4, [a["action"] for a in self.movements(conn)])

    async def test_loss_of_previously_seen_target_stops_without_search(self):
        conn = self.connection([[1, .5, .2, 1], [0, None, None, 1]])
        self.assertIn("暂时看不到", await self.run_navigation(conn))
        self.assertEqual(1, len(self.movements(conn)))

    async def test_obstruction_and_unknown_identity_produce_no_motion(self):
        for view in ([1, .5, .2, 0], [1, None, .2, 1], [None, None, None, 1]):
            conn = self.connection([view])
            answer = await self.run_navigation(conn)
            self.assertIn("停", answer)
            self.assertFalse(self.movements(conn))

    async def test_forward_limit_cannot_be_extended_by_model(self):
        conn = self.connection([[1, .5, .2, 1]] * 8)
        self.assertIn("步数上限", await self.run_navigation(conn))
        self.assertEqual(6, len(self.movements(conn)))

    async def test_close_off_center_target_never_triggers_another_step(self):
        conn = self.connection([[1, .1, .6, 1]])
        self.assertIn("不在正前方", await self.run_navigation(conn))
        self.assertFalse(self.movements(conn))

    async def test_marker_mapping_is_explicit_and_unmapped_target_cannot_move(self):
        options = {"identity_mode": "marker", "targets": {"感冒药": "蓝色瓶子"}}
        conn = self.connection([[1, .5, .5, 1]] * 2, options)
        await self.run_navigation(conn)
        question = next(args["question"] for name, args in conn.calls if name == "look_at_camera")
        self.assertIn("Find 蓝色瓶子", question)
        self.assertIn("unique marker", question)
        conn = self.connection([], options)
        self.assertIn("还没有配置", await self.run_navigation(conn, "退烧药"))
        self.assertFalse(conn.calls)

    async def test_camera_error_and_stale_generation_never_move(self):
        for stale in (False, True):
            conn = self.connection([])
            original = conn.func_handler.handle_llm_function_call.side_effect
            async def fail_capture(c, data):
                if data["name"] == "look_at_camera":
                    if stale:
                        conn.sentence_id = "new-turn"
                        return ActionResponse(Action.REQLLM, result=photo([1, .5, .2, 1]))
                    raise TimeoutError()
                return await original(c, data)
            conn.func_handler.handle_llm_function_call.side_effect = fail_capture
            if stale:
                with self.assertRaises(asyncio.CancelledError):
                    await self.run_navigation(conn)
            else:
                self.assertIn("异常", await self.run_navigation(conn))
            self.assertFalse(self.movements(conn))
            self.assertEqual("self_otto_stop", conn.calls[-1][0])

    async def test_abort_during_capture_cancels_and_stops(self):
        conn = self.connection([])
        entered = asyncio.Event()
        original = conn.func_handler.handle_llm_function_call.side_effect
        async def capture(c, data):
            if data["name"] == "look_at_camera":
                entered.set()
                await asyncio.Event().wait()
            return await original(c, data)
        conn.func_handler.handle_llm_function_call.side_effect = capture
        with patch("core.providers.tools.camera_routing.prepare_camera_request", new=AsyncMock()), \
             patch("core.providers.tools.medicine_navigation.asyncio.sleep", new=AsyncMock()):
            task = asyncio.create_task(navigate(conn, {"target": "感冒药"}, "turn", Mock()))
            await asyncio.wait_for(entered.wait(), .5)
            await stop_navigation(conn)
        self.assertTrue(task.cancelled())
        self.assertEqual("self_otto_stop", conn.calls[-1][0])
        self.assertFalse(self.movements(conn))

    async def test_failed_stop_is_not_reported_as_arrival(self):
        conn = self.connection([[1, .5, .5, 1]] * 2)
        original = conn.func_handler.handle_llm_function_call.side_effect
        async def broken_stop(c, data):
            if data["name"] == "self_otto_stop" and len(conn.calls) > 1:
                return ActionResponse(Action.ERROR, response="offline")
            return await original(c, data)
        conn.func_handler.handle_llm_function_call.side_effect = broken_stop
        self.assertIn("没有确认停止", await self.run_navigation(conn))

    async def test_missing_tools_and_disabled_mode_never_capture(self):
        for disabled in (True, False):
            conn = self.connection([], {"enabled": not disabled})
            if not disabled:
                conn.func_handler.has_tool = lambda name: False
            await self.run_navigation(conn)
            self.assertFalse(conn.calls)

    async def test_explicit_stop_does_not_need_camera(self):
        conn = self.connection([])
        conn.func_handler.has_tool = lambda name: name == "self_otto_stop"
        answer = await navigate(conn, {"stop": True}, "turn", Mock())
        self.assertIn("停止指令", answer)
        self.assertEqual(["self_otto_stop"], [name for name, _ in conn.calls])

    async def test_motion_acknowledgement_failure_stops_without_another_capture(self):
        conn = self.connection([[1, .5, .2, 1]])
        original = conn.func_handler.handle_llm_function_call.side_effect
        async def reject_action(c, data):
            if data["name"] == "self_otto_action":
                return ActionResponse(Action.REQLLM, result="错误：无效动作")
            return await original(c, data)
        conn.func_handler.handle_llm_function_call.side_effect = reject_action
        self.assertIn("停止寻药", await self.run_navigation(conn))
        self.assertEqual(1, sum(name == "look_at_camera" for name, _ in conn.calls))
        self.assertEqual("self_otto_stop", conn.calls[-1][0])


class ChatIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def connection(self):
        return SimpleNamespace(memory=None, loop=asyncio.get_running_loop(), config={},
            sentence_id="turn", client_abort=False, logger=Mock(), dialogue=Dialogue(),
            intent_type="function_call", func_handler=Mock(),
            tts=SimpleNamespace(tts_text_queue=queue.Queue(), store_tts_text=Mock()))

    async def test_real_chat_routes_asr_command_before_llm(self):
        path = Path(__file__).resolve().parents[2] / "core/connection.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ConnectionHandler")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "chat")
        scope = dict(Message=Message, uuid=uuid, TTSMessageDTO=TTSMessageDTO,
            SentenceType=SentenceType, ContentType=ContentType, TAG="test", enqueue_tool_report=Mock())
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), scope)
        conn = self.connection()
        with patch("core.providers.tools.medicine_navigation.dispatch_medicine_request", return_value=True) as dispatch:
            self.assertTrue(scope["chat"](conn, json.dumps({"content": "帮我拿感冒药", "language": "zh"})))
        self.assertEqual({"target": "感冒药"}, dispatch.call_args.args[1])
        self.assertEqual(conn.sentence_id, dispatch.call_args.args[2])
        conn.func_handler.handle_llm_function_call.assert_not_called()

    async def test_real_chat_routes_the_reported_question_before_character_reply(self):
        path = Path(__file__).resolve().parents[2] / "core/connection.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ConnectionHandler")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "chat")
        scope = dict(Message=Message, uuid=uuid, TTSMessageDTO=TTSMessageDTO,
            SentenceType=SentenceType, ContentType=ContentType, TAG="test", enqueue_tool_report=Mock())
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), scope)
        conn = self.connection()
        conn.config["medicine_navigation"] = RequestTests.options
        query = json.dumps({"content": "海绵宝宝，你面前有三瓶药，哪一瓶是感冒药？", "language": "zh", "emotion": "😶"})
        with patch("core.providers.tools.medicine_navigation.dispatch_medicine_request", return_value=True) as dispatch:
            self.assertTrue(scope["chat"](conn, query))
        self.assertEqual({"target": "感冒药", "identify": True, "observe": True}, dispatch.call_args.args[1])
        conn.func_handler.handle_llm_function_call.assert_not_called()

    async def test_fixed_reply_and_last_audio_marker(self):
        conn = self.connection()
        with patch("core.providers.tools.medicine_navigation.navigate", new=AsyncMock(return_value="我看到感冒药就在前方，已经停下。")):
            self.assertTrue(await asyncio.to_thread(dispatch_medicine_request, conn, {"target": "感冒药"}, "turn", Mock()))
        self.assertEqual("我看到感冒药就在前方，已经停下。", conn.dialogue.dialogue[-1].content)
        self.assertEqual(SentenceType.MIDDLE, conn.tts.tts_text_queue.get_nowait().sentence_type)
        self.assertEqual(SentenceType.LAST, conn.tts.tts_text_queue.get_nowait().sentence_type)

    async def test_late_result_never_speaks_in_replacement_turn(self):
        conn = self.connection()
        async def late(*args):
            conn.sentence_id = "replacement"
            return "旧结果"
        with patch("core.providers.tools.medicine_navigation.navigate", new=AsyncMock(side_effect=late)):
            self.assertFalse(await asyncio.to_thread(dispatch_medicine_request, conn, {"target": "感冒药"}, "turn", Mock()))
        self.assertTrue(conn.tts.tts_text_queue.empty())
        self.assertFalse(conn.dialogue.dialogue)


class CameraCompatibilityTests(unittest.TestCase):
    def test_camera_patch_preserves_existing_prompt_and_navigation_contract(self):
        root = Path(__file__).resolve().parents[2]
        path = root / "robot-navigation/patch_camera_bridge.py"
        spec = importlib.util.spec_from_file_location("camera_patch", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = 'def _vision_prompt(question):\n    question = question.strip()\n    return "Photo: " + question\n'
        patched = module.patch_source(source)
        self.assertEqual(patched, module.patch_source(patched))
        scope = {"quote": quote}
        exec(compile(patched, str(path), "exec"), scope)
        function = scope["_vision_prompt"]
        self.assertEqual("Photo: 拍张照", function("拍张照"))
        prompt = navigation_prompt("蓝色瓶子", "marker")
        self.assertEqual(prompt[5:], function(prompt))
        with self.assertRaises(ValueError):
            function("NAV1 Find " + "药" * 100)


if __name__ == "__main__":
    unittest.main()

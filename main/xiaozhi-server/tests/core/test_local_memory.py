"""Behavioral regression tests; all models are deterministic fakes, no network."""

import asyncio
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from core.memory.engine import LocalMemoryEngine
from core.memory.store import LocalMemoryStore, now_iso
from core.utils.dialogue import Dialogue, Message


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "memory.sqlite3")
        self.store = LocalMemoryStore(self.path)

    def message(self, text, device="one", session="first", role="user", **kwargs):
        message = Message(role=role, content=text, **kwargs)
        self.store.record_message(device, session, message)
        return message

    def extract(self, message, content=None, device="one", **kwargs):
        item = {"content": content or message.content, "category": "event", "topic": "测试话题",
                "source_ids": [message.uniq_id], **kwargs}
        result = self.store.apply_extraction(device, [{"id": message.uniq_id}], {"memories": [item]},
                                             self.store.revision(device))
        self.assertTrue(result)
        return next(row for row in self.store.list_memories(device)["items"]
                    if row["content"] == item["content"] and row["status"] == "active")

    def test_restart_keeps_originals_and_all_memories_beyond_old_summary_budget(self):
        for i in range(35):
            message = self.message(f"第{i}次旅行，我去了一个很特别的地方，还记得那里的美食与朋友。")
            self.extract(message)
        restarted = LocalMemoryStore(self.path)
        self.assertEqual(35, restarted.list_memories("one")["total"])
        self.assertEqual(35, restarted.list_messages("one")["total"])
        self.assertIn("第0次旅行", restarted.list_memories("one", limit=100)["items"][-1]["content"])

    def test_device_isolation_with_concurrent_writes_and_same_message_id(self):
        def write(device):
            msg = self.message("宠物叫" + device, device=device, uniq_id="same-id")
            self.extract(msg, device=device)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(write, ["one", "two"]))
        self.assertEqual("宠物叫one", self.store.list_memories("one")["items"][0]["content"])
        self.assertEqual("宠物叫two", self.store.list_memories("two")["items"][0]["content"])
        self.assertEqual(0, self.store.list_memories("one", "two")["total"])
        other_id = self.store.list_memories("two")["items"][0]["id"]
        with self.assertRaises(KeyError):
            self.store.delete("one", other_id)
        with self.assertRaises(KeyError):
            self.store.edit("one", other_id, {"content": "串号"})
        with self.assertRaises(KeyError):
            self.store.detail("one", other_id)

    def test_duplicate_recording_and_save_do_not_duplicate_originals(self):
        message = self.message("我叫小明")
        self.store.record_message("one", "first", message)
        self.extract(message)
        self.extract(message)
        self.assertEqual(1, self.store.list_messages("one")["total"])
        self.assertEqual(1, self.store.list_memories("one")["total"])

    def test_examples_system_tools_and_empty_messages_are_not_archived(self):
        self.message("example", is_temporary=True)
        self.message("system", role="system")
        self.message("tool", role="tool")
        self.message("", role="assistant")
        self.message("call", role="assistant", tool_calls=[{}])
        self.assertEqual(0, self.store.list_messages("one")["total"])

    def test_json_asr_envelope_is_preserved_as_plain_original_text(self):
        self.message(json.dumps({"speaker": "小明", "content": "我的猫叫奶茶"}, ensure_ascii=False))
        self.assertEqual("我的猫叫奶茶", self.store.list_messages("one")["items"][0]["content"])

    def test_preference_changes_preserve_past_and_present(self):
        old = self.extract(self.message("我喜欢咖啡"), category="preference", topic="咖啡")
        new = self.extract(self.message("后来我戒咖啡了"), category="preference", topic="咖啡", supersedes=[old["id"]])
        states = {r["content"]: r["status"] for r in self.store.list_memories("one")["items"]}
        self.assertEqual("historical", states["我喜欢咖啡"])
        self.assertEqual("active", states[new["content"]])
        self.assertEqual(2, self.store.list_messages("one")["total"])

    def test_changing_back_to_previous_preference_can_create_another_history_entry(self):
        first = self.extract(self.message("我喝咖啡"), topic="咖啡")
        second = self.extract(self.message("我戒咖啡了"), topic="咖啡", supersedes=[first["id"]])
        third = self.extract(self.message("我又喝咖啡了"), "我喝咖啡", topic="咖啡", supersedes=[second["id"]])
        self.extract(self.message("我再次戒咖啡了"), "我戒咖啡了", topic="咖啡", supersedes=[third["id"]])
        self.assertEqual(4, self.store.list_memories("one")["total"])

    def test_correction_keeps_audit_but_suppresses_wrong_original_from_recall(self):
        msg = self.message("我叫小红")
        memory = self.extract(msg, topic="小红", keywords=["小红"])
        revised = self.store.edit("one", memory["id"], {"content": "用户叫小明"})
        self.assertEqual("我叫小红", revised["revisions"][0]["content"])
        self.assertEqual(msg.uniq_id, revised["sources"][0]["id"])
        self.assertEqual(0, self.store.list_messages("one", "小红", for_recall=True)["total"])
        self.assertEqual(1, self.store.list_messages("one")["total"])
        self.assertEqual(0, self.store.list_memories("one", "小红")["total"])
        self.assertEqual(1, self.store.list_memories("one", "小明")["total"])

    def test_delete_keeps_owner_archive_but_never_reextracts_or_recalls_old_source(self):
        msg = self.message("我的猫叫奶茶")
        memory = self.extract(msg)
        self.store.delete("one", memory["id"])
        self.assertEqual(0, self.store.list_memories("one")["total"])
        self.assertEqual(1, self.store.list_messages("one")["total"])
        self.assertEqual(0, self.store.list_messages("one", "奶茶", for_recall=True)["total"])
        payload = {"memories": [{"content": "用户养了一只叫奶茶的猫", "source_ids": [msg.uniq_id]}]}
        self.store.apply_extraction("one", [{"id": msg.uniq_id}], payload, self.store.revision("one"))
        self.assertEqual(0, self.store.list_memories("one")["total"])

    def test_manual_edit_wins_against_an_inflight_extraction(self):
        old = self.extract(self.message("我叫小红"))
        pending = self.message("我住重庆")
        revision = self.store.revision("one")
        self.store.edit("one", old["id"], {"content": "我叫小明"})
        result = self.store.apply_extraction("one", [{"id": pending.uniq_id}],
            {"memories": [{"content": "我叫小红", "source_ids": [pending.uniq_id]}]}, revision)
        self.assertFalse(result)
        self.assertEqual(1, len(self.store.pending("one")))

    def test_invalid_sources_and_malformed_extraction_are_atomic(self):
        msg = self.message("我喜欢猫")
        payload = {"memories": [{"content": "我喜欢猫", "source_ids": [msg.uniq_id]},
                                {"content": "编造的事实", "source_ids": ["foreign"]}]}
        with self.assertRaises(ValueError):
            self.store.apply_extraction("one", [{"id": msg.uniq_id}], payload, 0)
        self.assertEqual(0, self.store.list_memories("one")["total"])
        self.assertEqual(1, len(self.store.pending("one")))

    def test_assistant_statements_are_never_recalled_as_user_facts(self):
        self.message("你肯定喜欢喝咖啡", role="assistant")
        self.assertEqual([], self.store.pending("one"))
        self.assertEqual(0, self.store.list_messages("one", "咖啡", for_recall=True)["total"])
        self.assertEqual(1, self.store.list_messages("one")["total"])

    def test_extraction_context_resolves_short_reply_without_crossing_device_or_session(self):
        self.message("别的会话里的问题", session="other")
        self.message("另一个设备的问题", device="two", role="assistant")
        self.message("上次考试怎么样？", role="assistant")
        self.message("考得很好")
        context = self.store.conversation_context("one", self.store.pending("one"))
        # Only the first-session batch should be used when testing its context.
        pending = [row for row in self.store.pending("one") if row["session_id"] == "first"]
        context = self.store.conversation_context("one", pending)
        self.assertEqual(["上次考试怎么样？", "考得很好"], [row["content"] for row in context])

    def test_suppressed_original_and_its_reply_do_not_enter_extraction_context(self):
        old_message = self.message("我叫小红")
        old_memory = self.extract(old_message)
        self.message("你叫小红呀", role="assistant")
        self.store.edit("one", old_memory["id"], {"content": "用户叫小明"})
        self.message("我现在准备考试")
        context = self.store.conversation_context("one", self.store.pending("one"))
        self.assertNotIn("小红", json.dumps(context, ensure_ascii=False))

    def test_due_follow_up_only_in_later_session_with_cooldown_and_completion(self):
        due = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        memory = self.extract(self.message("昨天考试"), follow_up_at=due)
        self.assertIsNone(self.store.claim_follow_up("one", "first"))
        self.assertEqual(memory["id"], self.store.claim_follow_up("one", "second")["id"])
        self.assertIsNone(self.store.claim_follow_up("one", "second", cooldown_hours=0))
        self.assertIsNone(self.store.claim_follow_up("one", "third"))
        self.assertIsNone(self.store.claim_follow_up("two", "third"))
        message = self.message("考试已经结束了，考得很好")
        self.store.apply_extraction("one", [{"id": message.uniq_id}],
            {"memories": [], "completed_follow_ups": [memory["id"]]}, self.store.revision("one"))
        self.assertIsNone(self.store.claim_follow_up("one", "fourth", cooldown_hours=0))

    def test_future_event_does_not_prompt_a_follow_up_early(self):
        future = (datetime.now(timezone.utc) + timedelta(days=8)).isoformat()
        self.extract(self.message("下周考试"), follow_up_at=future)
        self.assertIsNone(self.store.claim_follow_up("one", "second"))

    def test_chinese_search_matches_old_fact_keywords_and_has_sqlite_fallback(self):
        self.extract(self.message("奶茶三岁了"), keywords=["宠物", "猫", "养猫"], topic="奶茶")
        self.assertEqual(1, self.store.list_memories("one", "我养的宠物")["total"])
        self.assertEqual(1, self.store.list_messages("one", "奶茶")["total"])
        self.store.fts = False
        self.assertEqual(1, self.store.list_memories("one", "奶茶")["total"])
        self.assertEqual(0, self.store.list_memories("one", "' OR 1=1 --")["total"])

    def test_legacy_import_once_per_device_and_deletion_survives_restart(self):
        self.store.import_summary("one", "用户喜欢猫")
        self.store.import_summary("one", "用户喜欢猫")
        memory = self.store.list_memories("one")["items"][0]
        self.assertEqual([], self.store.detail("one", memory["id"])["sources"])
        self.store.delete("one", memory["id"])
        LocalMemoryStore(self.path).import_summary("one", "用户喜欢猫")
        self.assertEqual(0, self.store.list_memories("one")["total"])

    def test_explicit_new_remember_can_restore_fact_but_not_old_source(self):
        old_message = self.message("喜欢猫")
        old = self.extract(old_message)
        self.store.delete("one", old["id"])
        new = self.message("重新记住我喜欢猫")
        self.store.remember("one", new.uniq_id, "喜欢猫")
        self.assertEqual(1, self.store.list_memories("one")["total"])
        self.assertEqual(1, self.store.list_messages("one", for_recall=True)["total"])


class FakeLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def response_no_stream(self, system, prompt, **kwargs):
        self.calls.append((system, json.loads(prompt)))
        return self.response(system, json.loads(prompt)) if callable(self.response) else self.response


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = LocalMemoryEngine({"database_path": str(Path(self.temp.name) / "memory.sqlite3"),
                                         "migrate_legacy": False, "query_rewrite": False,
                                         "extraction_idle_seconds": 0})
        self.engine.init_memory("one", None, session_id="first")
        self.engine.schedule_extraction = lambda: None

    def put(self, text, role="user"):
        message = Message(role, text)
        self.engine.record_message(message)
        return message

    def test_original_is_durable_before_model_call_and_extraction_retries_after_failure(self):
        msg = self.put("我的猫叫奶茶")
        self.assertEqual(1, self.engine.store.list_messages("one")["total"])
        self.engine.set_llm(FakeLLM("not json"))
        with self.assertLogs("core.memory.engine", level="ERROR"):
            asyncio.run(self.engine.save_memory([msg]))
        self.assertEqual(1, len(self.engine.store.pending("one")))
        def response(system, data):
            return json.dumps({"memories": [{"content": "用户的猫叫奶茶", "topic": "奶茶",
                "keywords": ["宠物", "猫"], "source_ids": [data["messages"][0]["id"]]}]}, ensure_ascii=False)
        self.engine.set_llm(FakeLLM(response))
        asyncio.run(self.engine.save_memory([msg]))
        self.assertEqual([], self.engine.store.pending("one"))
        self.assertIn("奶茶", asyncio.run(self.engine.query_memory("我的猫叫什么")))

    def test_long_original_is_extracted_in_full_and_committed_only_after_all_chunks(self):
        self.engine.config['extraction_batch_chars'] = 400
        text = '我喜欢茶。' * 160 + '最后我还喜欢牛奶。'
        message = self.put(text)
        model = FakeLLM('{"memories":[]}')
        self.engine.set_llm(model)
        self.engine._extract_pending()
        chunks = [data['messages'][0] for _, data in model.calls]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(row['content']) <= 400 for row in chunks))
        self.assertTrue(all(row['id'] == message.uniq_id for row in chunks))
        self.assertIn('最后我还喜欢牛奶。', chunks[-1]['content'])
        self.assertEqual([], self.engine.store.pending('one'))
        self.assertEqual(text, self.engine.store.list_messages('one')['items'][0]['content'])

    def test_failed_later_chunk_keeps_original_pending_without_partial_memories(self):
        self.engine.config['extraction_batch_chars'] = 400
        message = self.put('我喜欢茶。' * 160)
        calls = []
        def response(system, data):
            calls.append(data)
            if len(calls) > 1:
                raise RuntimeError('temporary failure')
            return json.dumps({'memories':[{'content':'用户喜欢茶','source_ids':[message.uniq_id]}]})
        self.engine.set_llm(FakeLLM(response))
        with self.assertRaises(RuntimeError):
            self.engine._extract_pending()
        self.assertEqual(0, self.engine.store.list_memories('one')['total'])
        self.assertEqual(message.uniq_id, self.engine.store.pending('one')[0]['id'])

    def test_recall_and_extraction_payloads_remain_small_with_large_archive(self):
        for index in range(20):
            message = self.put(f'话题{index}：' + '用户喜欢茶。' * 60)
            self.engine.store.remember('one', message.uniq_id, message.content, category='preference')
        message = self.put('我今天喜欢喝茶。')
        self.engine.set_llm(FakeLLM('{"memories":[]}'))
        self.engine._extract_pending()
        self.assertLess(len(json.dumps(self.engine.llm.calls[-1][1], ensure_ascii=False)), 4500)
        self.assertLessEqual(len(self.engine.recall('我喜欢什么？')), 3600)
        self.assertEqual(20, self.engine.store.list_memories('one')['total'])
        self.assertEqual(21, self.engine.store.list_messages('one')['total'])

    def test_voice_remember_list_correct_and_forget(self):
        self.engine.set_llm(FakeLLM('{"action":"remember","content":"用户的猫叫奶茶"}'))
        msg = self.put("记住我的猫叫奶茶")
        self.assertEqual([], self.engine.store.pending("one"))
        self.assertIn("记住了", self.engine.handle_command(msg, [msg]))
        memory_id = self.engine.store.list_memories("one")["items"][0]["id"]
        self.engine.set_llm(FakeLLM('{"action":"list","query":""}'))
        self.assertIn("奶茶", self.engine.handle_command(self.put("你记得我什么"), []))
        self.engine.set_llm(FakeLLM(json.dumps({"action": "update", "ids": [memory_id], "content": "用户的猫叫布丁"})))
        self.assertIn("更正", self.engine.handle_command(self.put("你记错了，我的猫叫布丁"), []))
        self.assertTrue(self.engine.consume_context_reset())
        self.engine.set_llm(FakeLLM(json.dumps({"action": "delete", "ids": [memory_id]})))
        self.assertIn("忘掉", self.engine.handle_command(self.put("忘掉我的猫叫什么"), []))
        recalled = self.engine.recall("我的猫叫什么")
        self.assertNotIn("布丁", recalled)
        self.assertNotIn("奶茶", recalled)

    def test_ambiguous_delete_and_invented_id_do_not_mutate_memories(self):
        msg = self.put("我的猫叫奶茶")
        self.engine.store.remember("one", msg.uniq_id, msg.content)
        self.engine.set_llm(FakeLLM('{"action":"delete","ids":["invented"]}'))
        self.assertIn("具体", self.engine.handle_command(self.put("忘掉这件事"), []))
        self.assertEqual(1, self.engine.store.list_memories("one")["total"])

    def test_api_agent_summary_is_not_imported_into_separate_devices(self):
        self.engine.init_memory("two", None, summary_memory="共享的智能体摘要", save_to_file=False)
        self.assertEqual(0, self.engine.store.list_memories("two")["total"])

    def test_ordinary_forgetting_and_natural_change_are_not_memory_commands(self):
        for text in ["我忘记带钥匙了", "我现在戒咖啡了", "小说里的主角忘记了过去"]:
            self.assertFalse(self.engine.is_command(text))

    def test_explicit_remember_preserves_chat_context_and_can_schedule_plan_follow_up(self):
        due = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.engine.set_llm(FakeLLM(json.dumps({"action": "remember", "content": "用户昨天参加数学考试",
            "category": "plan", "topic": "数学考试", "keywords": ["考试"], "follow_up_at": due})))
        msg = self.put("记住我昨天参加了数学考试")
        self.assertIn("记住了", self.engine.handle_command(msg, [msg]))
        self.assertFalse(self.engine.consume_context_reset())
        self.engine.session_id = "second"
        self.assertIn("本次聊天可自然关心一次", self.engine.recall("你好"))

    def test_dialogue_hook_immediately_records_without_example_contamination(self):
        dialogue = Dialogue()
        dialogue.on_message = self.engine.record_message
        dialogue.put(Message("user", "这是示例", is_temporary=True))
        dialogue.put(Message("user", "这是实际聊天"))
        self.assertEqual(1, self.engine.store.list_messages("one")["total"])

    def test_memory_injection_works_without_placeholder_and_with_backslashes(self):
        dialogue = Dialogue()
        dialogue.update_system_message("你是小智")
        memory = r"用户的路径是 C:\new\team\1"
        messages = dialogue.get_llm_dialogue_with_memory(memory)
        self.assertIn(memory, messages[0]["content"])
        dialogue.update_system_message("你是小智 <memory></memory>")
        self.assertIn(memory, dialogue.get_llm_dialogue_with_memory(memory)[0]["content"])
        self.assertIn(memory, Dialogue().get_llm_dialogue_with_memory(memory)[0]["content"])

    def test_synonym_expansion_finds_an_old_event_outside_the_recent_profile(self):
        msg = self.put("那次去探望了奶茶")
        self.engine.store.apply_extraction("one", [{"id": msg.uniq_id}], {"memories": [{
            "content": "用户曾探望宠物猫奶茶", "category": "event", "topic": "奶茶",
            "source_ids": [msg.uniq_id]}]}, 0)
        self.engine.config["query_rewrite"] = True
        self.engine.set_llm(FakeLLM('{"keywords":["宠物","猫","奶茶"]}'))
        self.assertIn("用户曾探望宠物猫奶茶", self.engine.recall("你记得那只毛孩子吗"))

    def test_direct_local_hit_does_not_call_model_before_answering(self):
        msg = self.put("我最喜欢喝拿铁")
        self.engine.store.remember("one", msg.uniq_id, "用户最喜欢喝拿铁", category="preference")
        self.engine.config["query_rewrite"] = True
        self.engine.set_llm(FakeLLM('{"keywords":["咖啡"]}'))
        self.assertIn("用户最喜欢喝拿铁", self.engine.recall("我最喜欢喝什么？"))
        self.assertEqual([], self.engine.llm.calls)

    def test_generic_recall_word_alone_does_not_disable_synonym_expansion(self):
        msg = self.put("以前去爬过山")
        self.engine.store.remember("one", msg.uniq_id, "用户以前去爬过山")
        self.engine.config["query_rewrite"] = True
        self.engine.set_llm(FakeLLM('{"keywords":["爬山"]}'))
        self.engine.recall("以前那只毛孩子怎么样？")
        self.assertEqual(1, len(self.engine.llm.calls))

    def test_rewrite_can_be_forced_even_for_a_local_match(self):
        msg = self.put("我的猫叫奶茶")
        self.engine.store.remember("one", msg.uniq_id, "用户的猫叫奶茶")
        self.engine.config.update(query_rewrite=True, query_rewrite_on_miss=False)
        self.engine.set_llm(FakeLLM('{"keywords":["猫"]}'))
        self.engine.recall("我的猫叫什么？")
        self.assertEqual(1, len(self.engine.llm.calls))

    def test_unrelated_preference_does_not_hide_a_pet_synonym_query(self):
        msg = self.put("我喜欢咖啡")
        self.engine.store.remember("one", msg.uniq_id, "用户喜欢咖啡", category="preference")
        pet = self.put("探望了奶茶")
        self.engine.store.remember("one", pet.uniq_id, "用户曾探望宠物猫奶茶", category="event")
        self.engine.config["query_rewrite"] = True
        self.engine.set_llm(FakeLLM('{"keywords":["宠物","猫","奶茶"]}'))
        self.assertIn("用户曾探望宠物猫奶茶", self.engine.recall("你记得我喜欢的那只毛孩子吗？"))
        self.assertEqual(1, len(self.engine.llm.calls))

    def test_new_user_turn_cancels_deferred_extraction_without_losing_originals(self):
        del self.engine.schedule_extraction
        self.engine.config["extraction_idle_seconds"] = 10
        self.engine.llm = FakeLLM('{"memories":[]}')
        timers = []
        def timer_factory(delay, callback):
            timer = Mock()
            timer.callback = callback
            timers.append(timer)
            return timer
        with patch("core.memory.engine.threading.Timer", side_effect=timer_factory):
            self.put("我喜欢喝茶")
            self.put("记住啦", role="assistant")
            self.assertEqual([], self.engine.llm.calls)
            self.assertEqual(2, self.engine.store.list_messages("one")["total"])
            self.put("那你记得什么？")
            timers[0].cancel.assert_called_once()
            # A canceled callback waking up late must not start model work.
            with patch.object(self.engine, "_start_extraction") as start:
                timers[0].callback()
                start.assert_not_called()
                self.put("你喜欢茶", role="assistant")
                timers[-1].callback()
                start.assert_called_once()
            self.assertEqual(2, len(self.engine.store.pending("one")))

    def test_disconnect_archives_immediately_and_defers_model_work(self):
        self.engine.config["extraction_idle_seconds"] = 10
        self.engine.llm = FakeLLM('{"memories":[]}')
        schedule = Mock()
        self.engine.schedule_extraction = schedule
        message = Message("user", "我喜欢喝茶")
        asyncio.run(self.engine.save_memory([message]))
        self.assertEqual(message.uniq_id, self.engine.store.pending("one")[0]["id"])
        self.assertEqual([], self.engine.llm.calls)
        schedule.assert_called_once()

    def test_reconnected_device_cancels_old_sessions_timer_without_affecting_other_devices(self):
        del self.engine.schedule_extraction
        self.engine.config["extraction_idle_seconds"] = 10
        self.engine.llm = FakeLLM('{"memories":[]}')
        timer = Mock()
        with patch("core.memory.engine.threading.Timer", return_value=timer):
            self.put("我喜欢茶")
            self.put("记住了", role="assistant")
            other = LocalMemoryEngine(self.engine.config)
            other.init_memory("two", None, session_id="other-device")
            other.record_message(Message("user", "你好"))
            timer.cancel.assert_not_called()
            other.init_memory("one", None, session_id="reconnected")
            other.record_message(Message("user", "你记得我什么？"))
            timer.cancel.assert_called_once()

    def test_background_backlog_yields_after_one_batch_then_reschedules_remaining_originals(self):
        for index in range(5):
            self.put(f"第{index}条真实话语")
        self.engine.llm = FakeLLM('{"memories":[]}')
        self.engine.schedule_extraction = Mock()
        lock = threading.Lock()
        lock.acquire()
        self.engine._extract_worker(lock)
        self.assertEqual(1, len(self.engine.store.pending("one")))
        self.engine.schedule_extraction.assert_called_once()
        self.assertEqual(5, self.engine.store.list_messages("one")["total"])
        lock.acquire()
        self.engine._extract_worker(lock)
        self.assertEqual([], self.engine.store.pending("one"))

    def test_deleting_fact_suppresses_related_assistant_reply_but_archive_remains_searchable(self):
        msg = self.put("我的猫叫奶茶")
        self.engine.store.remember("one", msg.uniq_id, "用户的猫叫奶茶")
        self.put("原来你的猫叫奶茶呀", role="assistant")
        memory = self.engine.store.list_memories("one")["items"][0]
        self.engine.store.delete("one", memory["id"])
        self.assertNotIn("奶茶", self.engine.recall("你之前说过我的猫叫什么"))
        self.assertEqual(2, self.engine.store.list_messages("one", "奶茶")["total"])


if __name__ == "__main__":
    unittest.main()

"""Fiction continuity, ownership, and bounded local recall without model calls."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from core.character.stories import CharacterStoryBook, character_prompt
from core.character.routing import story_only_request
from core.memory.engine import LocalMemoryEngine
from core.memory.store import LocalMemoryStore
from core.utils.dialogue import Message


OPTIONS = {"enabled": True, "profile": "characters/spongebob.json", "recall_max_chars": 1200}
STORY = "我和派大星在比奇堡做了一辆纸风车，带到水母田，让小水母从风车旁游过。回家时我把风车放在菠萝屋的窗边。"
SEQUEL = "派大星后来又来到菠萝屋，我们给纸风车添了一颗星星。小蜗绕着风车转了一圈，它还留在窗边，等下次一起带去水母田。"


class CharacterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = LocalMemoryStore(str(Path(self.temp.name) / "memory.db"))
        self.book = CharacterStoryBook(self.store, "one", OPTIONS)

    def say(self, role, content, **kwargs):
        message = Message(role, content, **kwargs)
        self.store.record_message("one", "session", message)
        (self.book.on_user if role == "user" else self.book.on_assistant)(message)
        return message

    def test_seed_is_device_scoped_idempotent_and_deleted_seed_stays_deleted(self):
        one = {item["id"] for item in self.book.list()["items"]}
        self.assertEqual(3, len(one))
        self.assertEqual(one, {item["id"] for item in CharacterStoryBook(self.store, "one", OPTIONS).list()["items"]})
        other = CharacterStoryBook(self.store, "two", OPTIONS)
        self.assertTrue(one.isdisjoint(item["id"] for item in other.list()["items"]))
        identity = next(iter(one))
        with self.assertRaises(KeyError):
            other.edit(identity, {"content": "串设备"})
        self.book.delete(identity)
        self.assertEqual(2, CharacterStoryBook(self.store, "one", OPTIONS).list()["total"])
        self.assertEqual(3, other.list()["total"])

    def test_story_saved_once_and_continuation_keeps_one_story(self):
        self.say("user", json.dumps({"content": "给我讲一个你和派大星的新故事"}, ensure_ascii=False))
        self.book.context("给我讲一个你和派大星的新故事")
        first = self.say("assistant", STORY)
        self.book.on_assistant(first)
        identity = self.book.focus
        self.say("user", "后来呢？")
        self.assertIn("纸风车", self.book.context("后来呢？"))
        second = self.say("assistant", SEQUEL)
        self.book.on_assistant(second)
        self.assertEqual(4, self.book.list()["total"])
        row = self.book.detail(identity)
        self.assertIn(STORY, row["content"])
        self.assertIn(SEQUEL, row["content"])
        self.assertEqual(2, len(row["sources"]))
        self.assertEqual(0, self.store.list_memories("one")["total"])

    def test_restart_recalls_saved_story_and_continues_it(self):
        self.say("user", "给我编一个新故事")
        self.say("assistant", STORY)
        identity = self.book.focus
        self.book = CharacterStoryBook(LocalMemoryStore(self.store.path), "one", OPTIONS)
        self.say("user", "继续上次你和派大星的纸风车故事")
        context = self.book.context("继续上次你和派大星的纸风车故事")
        self.assertIn("窗边", context)
        self.say("assistant", SEQUEL)
        self.assertEqual(identity, self.book.focus)
        self.assertEqual(4, self.book.list()["total"])

    def test_short_continuation_can_use_pronouns_without_repeating_names(self):
        self.say("user", "给我讲个新故事")
        self.say("assistant", STORY)
        identity = self.book.focus
        self.say("user", "后来呢？")
        self.book.context("后来呢？")
        self.say("assistant", "主人，小智现在有点忙，我们稍后再试吧")
        self.assertNotIn("小智现在有点忙", self.book.detail(identity)["content"])
        self.say("assistant", "后来我们把它放进了纸盒，等明天出门再一起带上。")
        self.assertIn("放进了纸盒", self.book.detail(identity)["content"])

    def test_named_prop_can_be_recalled_without_character_name(self):
        context = self.book.context("那条蓝丝带后来怎么样了？")
        self.assertIn("网柄", context)
        self.assertIn("本机原创", context)

    def test_recalling_a_past_story_does_not_invent_or_append_a_new_chapter(self):
        self.say("user", "还记得上次水母田的蓝丝带故事吗？")
        context = self.book.context("还记得上次水母田的蓝丝带故事吗？")
        self.assertIn("回顾已经保存", context)
        self.assertNotIn("本轮任务：续写", context)
        identity = self.book.focus
        original = self.book.detail(identity)["content"]
        self.say("assistant", "我和派大星去过水母田，那条蓝丝带后来系在网柄上。我们安静观察小水母后把它放回了水母群。")
        self.assertEqual(original, self.book.detail(identity)["content"])
        self.assertEqual(3, self.book.list()["total"])
        self.say("user", "后来呢？")
        self.assertIn("本轮任务：续写", self.book.context("后来呢？"))

    def test_unrelated_turn_breaks_implicit_continuation_and_new_story_is_separate(self):
        self.say("user", "讲个故事")
        self.say("assistant", STORY)
        self.say("user", "今天几号？")
        self.assertEqual("", self.book.context("今天几号？"))
        self.say("user", "后来呢？")
        self.say("assistant", SEQUEL)
        self.assertEqual(4, self.book.list()["total"])
        self.say("user", "换一个新的比奇堡故事")
        self.say("assistant", SEQUEL)
        self.assertEqual(5, self.book.list()["total"])

    def test_tool_examples_and_regular_replies_do_not_become_stories(self):
        self.say("user", "你今天能帮我写代码吗？")
        self.say("assistant", STORY)
        self.assertEqual(3, self.book.list()["total"])

    def test_daily_life_requests_do_not_require_the_literal_word_story(self):
        for query in ["给我讲一个你和派大星的新日常", "讲一个你在蟹堡王的新日常"]:
            self.book.on_user(Message("user", query))
            self.assertTrue(self.book.story_turn)
            self.assertIn("创作新的日常", self.book.context(query))

    def test_story_fast_path_keeps_device_and_camera_requests_on_tool_path(self):
        for text in ["给我讲一个你和派大星的新日常", "继续上次纸风车的故事", "后来呢？",
                     "海绵宝宝，讲个故事，100字左右"]:
            self.assertTrue(story_only_request(text), text)
        for text in ["打开摄像头看看我，然后讲故事", "讲个故事，再把音量调小", "向前走两步",
                     "给我看一下摄像头", "开灯并讲个故事", "用摄像头看看那个玩具再编个故事"]:
            self.assertFalse(story_only_request(text), text)
        self.say("user", "讲一个故事")
        self.say("assistant", STORY, tool_calls=[{"id": "tool"}])
        self.say("assistant", STORY, is_temporary=True)
        self.say("assistant", "主人，小智现在有点忙")
        self.assertEqual(3, self.book.list()["total"])

    def test_edit_and_delete_preserve_originals_but_drop_obsolete_recall(self):
        user = self.say("user", "我明天考试，讲个比奇堡故事鼓励我")
        answer = self.say("assistant", STORY)
        identity = self.book.focus
        revision = self.store.context_revision("one")
        edited = self.book.edit(identity, {"content": "我和派大星把纸风车改放在书架上。"})
        self.assertEqual(STORY, edited["revisions"][0]["content"])
        self.assertGreater(self.store.context_revision("one"), revision)
        originals = {row["id"]: row for row in self.store.list_messages("one")["items"]}
        self.assertEqual(STORY, originals[answer.uniq_id]["content"])
        self.assertTrue(originals[answer.uniq_id]["excluded"])
        self.assertFalse(originals[user.uniq_id]["excluded"])
        self.assertEqual(1, len(self.store.pending("one")))
        self.assertEqual(0, self.store.list_messages("one", "纸风车", for_recall=True, include_assistant=True)["total"])
        self.book.delete(identity)
        with self.assertRaises(KeyError):
            self.book.detail(identity)
        self.assertEqual(2, self.store.list_messages("one")["total"])

    def test_long_story_is_archived_in_full_but_recall_and_identity_are_bounded(self):
        content = STORY * 700
        identity = self.book.create(content, "纸风车的冒险")
        context = self.book.context("继续上次纸风车的冒险故事", max_chars=500)
        self.assertLessEqual(len(context), 500)
        self.assertIn("全文在本机", context)
        self.assertEqual(content, self.book.detail(identity)["content"])
        prompt = character_prompt("你是小智。", OPTIONS)
        self.assertEqual(prompt, character_prompt(prompt, OPTIONS))
        self.assertLess(len(prompt), 650)
        self.assertEqual("原有角色", character_prompt("原有角色", {}))

    def test_engine_uses_local_story_recall_and_keeps_real_facts_separate(self):
        engine = LocalMemoryEngine({"database_path": self.store.path, "migrate_legacy": False, "character": OPTIONS})
        model = Mock()
        model.response_no_stream.side_effect = AssertionError("story recall must not call model")
        engine.init_memory("one", model)
        engine.schedule_extraction = lambda: None
        engine.record_message(Message("user", "给我讲一个你和派大星的新故事"))
        engine.record_message(Message("assistant", STORY))
        recalled = engine.recall("继续上次你和派大星的故事")
        self.assertIn("纸风车", recalled)
        self.assertIn("不是用户现实经历", recalled)
        self.assertLessEqual(len(recalled), 3600)
        self.assertEqual(0, engine.store.list_memories("one")["total"])
        model.response_no_stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()

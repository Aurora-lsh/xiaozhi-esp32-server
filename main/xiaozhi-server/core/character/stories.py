"""Persist character fiction separately from sourced memories about the user."""

import json
import re
import uuid
from functools import lru_cache
from pathlib import Path

from core.memory.store import PROJECT_DIR, now_iso, search_tokens


@lru_cache(maxsize=8)
def load_profile(path):
    file = Path(path)
    if not file.is_absolute():
        file = PROJECT_DIR / file
    profile = json.loads(file.read_text(encoding="utf-8"))
    if not profile.get("id") or not isinstance(profile.get("identity_prompt"), str):
        raise ValueError("角色资料缺少身份或背景")
    return profile


def character_prompt(prompt, options):
    if not options.get("enabled", False):
        return prompt
    profile = load_profile(options.get("profile", "characters/spongebob.json"))
    # PromptManager may return an enhanced prompt previously cached for a device.
    prompt = re.sub(r"\n?<character_identity>.*?</character_identity>", "", prompt, flags=re.S)
    return prompt + "\n<character_identity>\n" + profile["identity_prompt"] + "\n</character_identity>"


def plain_text(value):
    try:
        wrapped = json.loads(value)
        if isinstance(wrapped, dict) and isinstance(wrapped.get("content"), str):
            return wrapped["content"]
    except (ValueError, TypeError):
        pass
    return value if isinstance(value, str) else ""


class CharacterStoryBook:
    def __init__(self, store, device, options):
        self.store, self.device, self.options = store, device, options
        self.profile = load_profile(options.get("profile", "characters/spongebob.json"))
        self.character = self.profile["id"]
        self.story_turn = self.continuation = self.recollection = False
        self.focus = None
        self.query = ""
        self._prepared = False
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS character_stories (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, character_id TEXT NOT NULL,
                    title TEXT NOT NULL, content TEXT NOT NULL, origin TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS character_story_device
                    ON character_stories(device_id, character_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS character_story_sources (
                    story_id TEXT NOT NULL, message_id TEXT NOT NULL, device_id TEXT NOT NULL,
                    PRIMARY KEY(device_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS character_story_revisions (
                    story_id TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
                    changed_at TEXT NOT NULL, reason TEXT NOT NULL
                );
            """)
            db.execute("INSERT OR IGNORE INTO devices(device_id) VALUES (?)", (device,))
            for seed in self.profile.get("seeds", []):
                identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{device}/{self.character}/{seed['id']}"))
                stamp = now_iso()
                db.execute("""INSERT OR IGNORE INTO character_stories
                    (id,device_id,character_id,title,content,origin,created_at,updated_at)
                    VALUES (?,?,?,?,?,'original_seed',?,?)""",
                    (identity, device, self.character, seed["title"], seed["content"], stamp, stamp))

    def _rows(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM character_stories
                WHERE device_id=? AND character_id=? AND status='active'
                ORDER BY updated_at DESC,id""", (self.device, self.character))]

    def list(self, query="", limit=30, offset=0):
        rows = self._rows()
        if query:
            tokens = [token for token in search_tokens(query) if len(token) > 1]
            ranked = [(sum(token in (row["title"] + row["content"]).lower() for token in tokens), row)
                      for row in rows]
            rows = [row for score, row in sorted(ranked, key=lambda item: -item[0]) if score]
        return {"items": rows[offset:offset + limit], "total": len(rows)}

    def _get(self, db, identity):
        row = db.execute("""SELECT * FROM character_stories WHERE id=? AND device_id=?
            AND character_id=? AND status='active'""", (identity, self.device, self.character)).fetchone()
        if row is None:
            raise KeyError("故事不存在")
        return dict(row)

    def detail(self, identity):
        with self.store.connect() as db:
            row = self._get(db, identity)
            row["sources"] = [dict(item) for item in db.execute("""SELECT m.* FROM messages m
                JOIN character_story_sources s ON m.id=s.message_id AND m.device_id=s.device_id
                WHERE s.story_id=? AND s.device_id=? ORDER BY m.seq""", (identity, self.device))]
            row["revisions"] = [dict(item) for item in db.execute("""SELECT title,content,changed_at,reason
                FROM character_story_revisions WHERE story_id=? ORDER BY changed_at""", (identity,))]
            return row

    @staticmethod
    def _validate(content, title):
        if not isinstance(content, str) or not content.strip() or len(content) > 100000:
            raise ValueError("故事内容须为1至100000字")
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ValueError("故事标题须为1至120字")
        return content.strip(), title.strip()

    @staticmethod
    def _title(content):
        return re.sub(r"[\r\n]+", " ", content).strip()[:36]

    def create(self, content, title="比奇堡的新日常", origin="manual", message=None):
        content, title = self._validate(content, title)
        identity, stamp = str(uuid.uuid4()), now_iso()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if message is not None:
                old = db.execute("SELECT story_id FROM character_story_sources WHERE device_id=? AND message_id=?",
                                 (self.device, message.uniq_id)).fetchone()
                if old:
                    return old[0]
            db.execute("""INSERT INTO character_stories
                (id,device_id,character_id,title,content,origin,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                (identity, self.device, self.character, title, content, origin, stamp, stamp))
            if message is not None:
                db.execute("INSERT INTO character_story_sources VALUES (?,?,?)", (identity, message.uniq_id, self.device))
        return identity

    def append(self, identity, message):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get(db, identity)
            if db.execute("SELECT 1 FROM character_story_sources WHERE device_id=? AND message_id=?",
                          (self.device, message.uniq_id)).fetchone():
                return
            db.execute("UPDATE character_stories SET content=?,updated_at=? WHERE id=?",
                       (row["content"] + "\n\n续篇：\n" + message.content, now_iso(), identity))
            db.execute("INSERT INTO character_story_sources VALUES (?,?,?)", (identity, message.uniq_id, self.device))

    def edit(self, identity, changes):
        if not isinstance(changes, dict) or set(changes) - {"content", "title"}:
            raise ValueError("只能修改故事内容与标题")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._get(db, identity)
            content, title = self._validate(changes.get("content", old["content"]), changes.get("title", old["title"]))
            if (content != old["content"] and title == old["title"]
                    and old["origin"] == "generated" and old["title"] == self._title(old["content"])):
                # An automatically excerpted title must not retain a corrected fact.
                title = self._title(content)
            self._revise(db, old, "edit")
            db.execute("UPDATE character_stories SET title=?,content=?,updated_at=? WHERE id=?",
                       (title, content, now_iso(), identity))
        return self.detail(identity)

    def delete(self, identity):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._get(db, identity)
            self._revise(db, old, "delete")
            db.execute("UPDATE character_stories SET status='deleted',updated_at=? WHERE id=?", (now_iso(), identity))

    def _revise(self, db, old, reason):
        db.execute("INSERT INTO character_story_revisions VALUES (?,?,?,?,?)",
                   (old["id"], old["title"], old["content"], now_iso(), reason))
        # Keep original text as an archive, but stop recalling an obsolete telling.
        db.execute("""UPDATE messages SET excluded=1 WHERE device_id=? AND id IN
            (SELECT message_id FROM character_story_sources WHERE story_id=? AND device_id=?)""",
            (self.device, old["id"], self.device))
        db.execute("UPDATE devices SET context_revision=context_revision+1 WHERE device_id=?", (self.device,))

    def on_user(self, message):
        query = plain_text(message.content)
        previously_story = self.story_turn
        continuing = bool(re.search(r"^(?:那|好[，,]?\s*)?(?:继续|接着|后来|然后|接下来|那个故事)", query))
        world = any(word in query for word in self.profile.get("world_terms", []))
        wants_story = bool(re.search(r"故事|经历|日常|发生|讲讲|说说|干了什么|做了什么|今天|昨天|上次", query))
        personal_story = bool(re.search(r"你.{0,12}(?:故事|经历|日常|小时候)|你上次讲|你(?:今天|昨天|以前).{0,8}(?:做|干|过得|发生|去哪|去哪里)", query))
        generic = bool(re.search(r"(?:讲|编|说)(?:一)?个(?:新|新的)?故事|上次.{0,6}故事|那个故事", query))
        fresh = bool(re.search(r"新(?:的)?故事|换一个|编一个|新日常", query))
        self.story_turn = generic or personal_story or (world and wants_story) or (continuing and previously_story)
        # A named prop can be recalled after reconnecting without a character name.
        if not self.story_turn and not re.search(r"我(?:的|今天|昨天|以前)", query) and re.search(r"还记得|后来|什么|哪里|哪儿|怎么样|颜色", query):
            hits = self.list(query, limit=1)["items"]
            excluded = set(search_tokens("还记得后来什么哪里哪儿怎么样颜色上次之前故事"))
            specific = [word for word in search_tokens(query) if len(word) > 1 and word not in excluded]
            self.story_turn = bool(hits and any(word in hits[0]["title"] for word in specific))
        self.continuation = self.story_turn and not fresh and (
            (continuing and previously_story) or bool(re.search(r"后来|接着|继续|接下来", query)))
        self.recollection = (self.story_turn and not fresh and not self.continuation
            and bool(re.search(r"还记得|上次|那次|讲过|以前|什么颜色|放在哪里|放哪儿", query)))
        if not self.continuation:
            self.focus = None
        self.query, self._prepared = query, True

    def context(self, query, max_chars=None):
        query = plain_text(query)
        if not self._prepared or query != self.query:
            from types import SimpleNamespace
            self.on_user(SimpleNamespace(content=query))
        if not self.story_turn:
            return ""
        fresh = bool(re.search(r"新(?:的)?故事|换一个|编一个|新日常", query))
        rows = [] if fresh else self.list(query, limit=2)["items"]
        if self.continuation and self.focus:
            try:
                rows = [self.detail(self.focus)]
            except KeyError:
                self.focus = None
        if not rows and (self.continuation or self.recollection) and not fresh:
            rows = self.list(limit=1)["items"]
        if (self.continuation or self.recollection) and rows:
            self.focus = rows[0]["id"]
        budget = max(300, min(1800, int(self.options.get("recall_max_chars", 1200))))
        if max_chars is not None:
            budget = min(budget, max(300, max_chars))
        introduction = "角色故事档案：以下是本机原创日常，不是动画官方剧情，也不是用户现实经历。保持已存人物、物品和经过一致；没有记录的细节可合理续写，不能编造动画集数。"
        if self.continuation:
            introduction = ("本轮任务：续写新情节。档案里的内容都已经讲过，直接从结尾之后开始讲新的行动，"
                "不要重述开场或复制已有段落。角色姓名是海绵宝宝，按用户要求的字数简短续讲。" + introduction)
        elif self.recollection:
            introduction = ("本轮任务：回顾已经保存的故事。根据记录回答问到的细节，不推进新情节，"
                "未记载的细节坦诚说明没有记录。" + introduction)
        if not rows:
            return introduction + "本轮可在比奇堡设定内创作新的日常，讲述内容会自动保存在本机。"
        excerpts = []
        per_story = max(150, (budget - len(introduction) - 160) // len(rows))
        for row in rows:
            content = row["content"]
            if len(content) > per_story:
                # Preserve both the beginning and the latest continuation.
                content = content[:per_story // 2] + "…（中间略，全文在本机档案）…" + content[-per_story // 2:]
            excerpts.append({"标题": row["title"], "已经讲过的内容": content})
        result = introduction + json.dumps(excerpts, ensure_ascii=False)
        while len(result) > budget and excerpts:
            longest = max(excerpts, key=lambda item: len(item["已经讲过的内容"]))
            excess = len(result) - budget
            longest["已经讲过的内容"] = longest["已经讲过的内容"][:max(0, len(longest["已经讲过的内容"]) - excess)]
            result = introduction + json.dumps(excerpts, ensure_ascii=False)
            if not longest["已经讲过的内容"] and len(result) > budget:
                excerpts.remove(longest)
                result = introduction + json.dumps(excerpts, ensure_ascii=False)
        return result

    def on_assistant(self, message):
        # Repeating a known telling is an answer, not a new story/chapter.
        if self.recollection and self.focus:
            return
        if (not self.story_turn or getattr(message, "is_temporary", False)
                or getattr(message, "tool_calls", None) or not isinstance(message.content, str)
                or "小智现在有点忙" in message.content
                or len(message.content) < (12 if self.continuation and self.focus else 35)
                or (not (self.continuation and self.focus)
                    and not any(word in message.content for word in self.profile.get("world_terms", [])))):
            return
        if self.continuation and self.focus:
            try:
                self.append(self.focus, message)
                return
            except KeyError:
                self.focus = None
        title = self._title(message.content)
        self.focus = self.create(message.content, title, "generated", message)

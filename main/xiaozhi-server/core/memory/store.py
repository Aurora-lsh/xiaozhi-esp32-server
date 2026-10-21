"""Durable memory and transcripts. No model or server dependencies are required."""

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
CATEGORIES = {"profile", "preference", "relationship", "event", "plan"}
CHINA_TZ = timezone(timedelta(hours=8))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_time(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def fingerprint(content):
    return hashlib.sha256(re.sub(r"\W", "", content).lower().encode()).hexdigest()


def search_tokens(content):
    tokens = []
    for chunk in re.findall(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+", content.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", chunk):
            tokens.extend(chunk)
            tokens.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
        else:
            tokens.append(chunk)
    return list(dict.fromkeys(tokens))


def database_path(config=None):
    path = Path((config or {}).get("database_path", "data/long_term_memory.sqlite3"))
    return str(path if path.is_absolute() else PROJECT_DIR / path)


class LocalMemoryStore:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
                    context_revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL, device_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0,
                    excluded INTEGER NOT NULL DEFAULT 0,
                    parent_user_id TEXT,
                    UNIQUE(device_id, id)
                );
                CREATE INDEX IF NOT EXISTS messages_device_time
                    ON messages(device_id, created_at, seq);
                CREATE INDEX IF NOT EXISTS messages_pending
                    ON messages(device_id, processed, excluded, role);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                    content TEXT NOT NULL, category TEXT NOT NULL, topic TEXT NOT NULL,
                    keywords TEXT NOT NULL DEFAULT '[]', event_time TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    follow_up_at TEXT, last_follow_up_at TEXT, last_follow_up_session TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memories_device_status
                    ON memories(device_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS memory_sources (
                    memory_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    PRIMARY KEY(memory_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS memory_revisions (
                    memory_id TEXT NOT NULL, content TEXT NOT NULL,
                    changed_at TEXT NOT NULL, reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blocked_memories (
                    device_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    PRIMARY KEY(device_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS imports (
                    device_id TEXT NOT NULL, name TEXT NOT NULL,
                    PRIMARY KEY(device_id, name)
                );
            """)
            if "context_revision" not in {row[1] for row in db.execute("PRAGMA table_info(devices)")}:
                db.execute("ALTER TABLE devices ADD COLUMN context_revision INTEGER NOT NULL DEFAULT 0")
            if "parent_user_id" not in {row[1] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN parent_user_id TEXT")
            # Pre-tokenizing Chinese gives FTS5 useful word fragments without an
            # embedding service, model download, or third-party tokenizer.
            try:
                db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS memory_search
                    USING fts5(device_id UNINDEXED, kind UNINDEXED,
                               ref_id UNINDEXED, tokens)""")
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _index(self, db, device, kind, ref, content):
        if self.fts:
            db.execute("DELETE FROM memory_search WHERE device_id=? AND kind=? AND ref_id=?",
                       (device, kind, ref))
            if content:
                db.execute("INSERT INTO memory_search VALUES (?, ?, ?, ?)",
                           (device, kind, ref, " ".join(search_tokens(content))))

    def record_message(self, device, session, message):
        if (not device or message.role not in ("user", "assistant")
                or getattr(message, "is_temporary", False)
                or not isinstance(message.content, str) or not message.content.strip()
                or getattr(message, "tool_calls", None)):
            return
        content = message.content
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
                content = parsed["content"]
        except (ValueError, TypeError):
            pass
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO devices(device_id) VALUES (?)", (device,))
            parent = None
            if message.role == "assistant":
                parent = db.execute("""SELECT id, excluded FROM messages WHERE device_id=?
                    AND session_id=? AND role='user' ORDER BY seq DESC LIMIT 1""", (device, session)).fetchone()
            result = db.execute("""INSERT OR IGNORE INTO messages
                (id, device_id, session_id, role, content, created_at, parent_user_id, excluded)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (message.uniq_id, device, session, message.role, content,
                 getattr(message, "created_at", None) or now_iso(),
                 parent["id"] if parent else None, parent["excluded"] if parent else 0))
            if result.rowcount:
                self._index(db, device, "message", message.uniq_id, content)

    def revision(self, device):
        with self.connect() as db:
            row = db.execute("SELECT revision FROM devices WHERE device_id=?", (device,)).fetchone()
            return row[0] if row else 0

    def context_revision(self, device):
        with self.connect() as db:
            row = db.execute("SELECT context_revision FROM devices WHERE device_id=?", (device,)).fetchone()
            return row[0] if row else 0

    def devices(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT d.device_id,
                (SELECT count(*) FROM memories m WHERE m.device_id=d.device_id
                    AND m.status!='deleted') AS memories,
                (SELECT count(*) FROM messages t WHERE t.device_id=d.device_id) AS messages
                FROM devices d ORDER BY d.device_id""")]

    def pending(self, device, limit=24):
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM messages
                WHERE device_id=? AND role='user' AND processed=0 AND excluded=0
                ORDER BY seq LIMIT ?""", (device, limit))]

    def conversation_context(self, device, batch):
        """Nearby past turns resolve short answers; never cross device/session."""
        context = {}
        with self.connect() as db:
            for message in batch:
                rows = db.execute("""SELECT * FROM messages WHERE device_id=?
                    AND session_id=? AND seq<=? AND excluded=0 ORDER BY seq DESC LIMIT 4""",
                    (device, message["session_id"], message["seq"]))
                for row in rows:
                    context[row["seq"]] = {key: row[key] for key in ("role", "content", "created_at")}
        return [context[key] for key in sorted(context)][-60:]

    def memories_for_topics(self, device, topics, limit=32):
        topics = list(topics)
        if not topics:
            return []
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memories WHERE device_id=? AND status!='deleted' AND topic IN ("
                + ",".join("?" for _ in topics) + ") ORDER BY updated_at DESC LIMIT ?", (device, *topics, limit))
            return [dict(row) for row in rows]

    def topics(self, device, limit=300):
        with self.connect() as db:
            return [row[0] for row in db.execute("""SELECT topic FROM memories WHERE device_id=?
                AND status!='deleted' AND topic!='' GROUP BY topic
                ORDER BY max(updated_at) DESC LIMIT ?""", (device, limit))]

    def exclude_message(self, device, message_id):
        with self.connect() as db:
            db.execute("UPDATE messages SET processed=1, excluded=1 WHERE device_id=? AND id=?",
                       (device, message_id))

    def _matching_ids(self, db, device, query, kind, limit):
        tokens = search_tokens(query)
        # Ignore single Chinese characters for multi-character queries to keep
        # common pronouns from dominating the ranking.
        useful = [t for t in tokens if len(t) > 1] or tokens
        if not useful:
            return []
        if self.fts:
            expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in useful[:80])
            return [row[0] for row in db.execute("""SELECT ref_id FROM memory_search
                WHERE memory_search MATCH ? AND device_id=? AND kind=?
                ORDER BY bm25(memory_search) LIMIT ?""", (expression, device, kind, limit))]
        table, key = ("memories", "id") if kind == "memory" else ("messages", "id")
        field = "(content || ' ' || topic || ' ' || keywords)" if kind == "memory" else "content"
        conditions = " OR ".join(field + " LIKE ? ESCAPE '\\'" for _ in useful[:80])
        escaped = ["%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                   for t in useful[:80]]
        return [row[0] for row in db.execute(
            f"SELECT {key} FROM {table} WHERE device_id=? AND ({conditions}) LIMIT ?",
            (device, *escaped, limit))]

    def list_memories(self, device, query="", limit=50, offset=0):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("""SELECT * FROM memories
                WHERE device_id=? AND status!='deleted' ORDER BY updated_at DESC, id""", (device,))]
            if query:
                ids = self._matching_ids(db, device, query, "memory", 10000)
                positions = {key: i for i, key in enumerate(ids)}
                rows = sorted((r for r in rows if r["id"] in positions), key=lambda r: positions[r["id"]])
            return {"items": rows[offset:offset + limit], "total": len(rows)}

    def list_messages(self, device, query="", session="", limit=50, offset=0, for_recall=False,
                      include_assistant=False):
        with self.connect() as db:
            where, args = ["device_id=?"], [device]
            if session:
                where.append("session_id=?")
                args.append(session)
            if for_recall:
                where.append("excluded=0")
                if not include_assistant:
                    where.append("role='user'")
            if query:
                ids = self._matching_ids(db, device, query, "message", 10000)
                if not ids:
                    return {"items": [], "total": 0}
                # Parameterize identifiers as values, never as SQL fragments.
                where.append("id IN (" + ",".join("?" for _ in ids) + ")")
                args.extend(ids)
            clause = " AND ".join(where)
            total = db.execute(f"SELECT count(*) FROM messages WHERE {clause}", args).fetchone()[0]
            rows = db.execute(f"SELECT * FROM messages WHERE {clause} ORDER BY seq DESC LIMIT ? OFFSET ?",
                              (*args, limit, offset))
            return {"items": [dict(row) for row in rows], "total": total}

    def detail(self, device, memory_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE device_id=? AND id=? AND status!='deleted'",
                             (device, memory_id)).fetchone()
            if not row:
                raise KeyError("记忆不存在")
            result = dict(row)
            result["sources"] = [dict(r) for r in db.execute("""SELECT t.* FROM messages t
                JOIN memory_sources s ON t.id=s.message_id
                WHERE t.device_id=? AND s.memory_id=? ORDER BY t.seq""", (device, memory_id))]
            result["revisions"] = [dict(r) for r in db.execute("""SELECT content, changed_at, reason
                FROM memory_revisions WHERE memory_id=? ORDER BY changed_at""", (memory_id,))]
            return result

    def _suppress_sources(self, db, device, memory_id):
        ids = [r[0] for r in db.execute("SELECT message_id FROM memory_sources WHERE memory_id=?", (memory_id,))]
        for source_id in ids:
            db.execute("""UPDATE messages SET excluded=1, processed=1
                WHERE device_id=? AND (id=? OR parent_user_id=?)""", (device, source_id, source_id))

    def remember(self, device, message_id, content, category="event", topic="用户明确要求记住",
                 keywords=None, event_time=None, follow_up_at=None):
        """An explicit new request may intentionally restore a forgotten fact."""
        if not isinstance(content, str) or not content.strip() or len(content) > 4000:
            raise ValueError("记忆内容须为1至4000字")
        content = content.strip()
        if category not in CATEGORIES or not isinstance(topic, str):
            raise ValueError("无效的记忆分类或话题")
        if not isinstance(keywords or [], list) or not all(isinstance(word, str) for word in keywords or []):
            raise ValueError("关键词必须为文字列表")
        if event_time:
            datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        follow_up_at = canonical_time(follow_up_at)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM messages WHERE device_id=? AND id=? AND role='user'",
                              (device, message_id)).fetchone():
                raise ValueError("找不到本次记忆请求的原文")
            db.execute("DELETE FROM blocked_memories WHERE device_id=? AND fingerprint=?", (device, fingerprint(content)))
            old = db.execute("SELECT id FROM memories WHERE device_id=? AND content=? AND status='active'",
                             (device, content)).fetchone()
            memory_id, created = (old[0] if old else str(uuid.uuid4())), now_iso()
            if not old:
                db.execute("""INSERT INTO memories
                    (id, device_id, content, category, topic, keywords, event_time,
                     follow_up_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (memory_id, device, content, category, topic[:200], json.dumps(keywords or [], ensure_ascii=False),
                     event_time, follow_up_at, created, created))
                self._index(db, device, "memory", memory_id, content + " " + topic + " " + " ".join(keywords or []))
            db.execute("INSERT OR IGNORE INTO memory_sources VALUES (?, ?)", (memory_id, message_id))
            db.execute("UPDATE devices SET revision=revision+1 WHERE device_id=?", (device,))
        return self.detail(device, memory_id)

    def edit(self, device, memory_id, changes):
        allowed = {"content", "category", "topic", "event_time", "follow_up_at", "status"}
        changes = {k: v for k, v in changes.items() if k in allowed}
        if not changes:
            raise ValueError("没有可修改的字段")
        if "content" in changes and (not isinstance(changes["content"], str)
                                     or not changes["content"].strip() or len(changes["content"]) > 4000):
            raise ValueError("记忆内容须为1至4000字")
        if "topic" in changes and (not isinstance(changes["topic"], str) or len(changes["topic"]) > 200):
            raise ValueError("话题须为不超过200字的文字")
        if "category" in changes and changes["category"] not in CATEGORIES:
            raise ValueError("无效的记忆分类")
        if "status" in changes and changes["status"] not in {"active", "historical"}:
            raise ValueError("无效的记忆状态")
        if "follow_up_at" in changes:
            changes["follow_up_at"] = canonical_time(changes["follow_up_at"])
        if changes.get("event_time"):
            datetime.fromisoformat(changes["event_time"].replace("Z", "+00:00"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM memories WHERE device_id=? AND id=? AND status!='deleted'",
                             (device, memory_id)).fetchone()
            if not old:
                raise KeyError("记忆不存在")
            if "content" in changes and changes["content"] != old["content"]:
                db.execute("INSERT INTO memory_revisions VALUES (?, ?, ?, 'correction')",
                           (memory_id, old["content"], now_iso()))
                db.execute("INSERT OR IGNORE INTO blocked_memories VALUES (?, ?)", (device, fingerprint(old["content"])))
                self._suppress_sources(db, device, memory_id)
                # Model-generated search tags may contain the very name being
                # corrected. They must not survive as apparently current facts.
                changes["keywords"] = "[]"
                if changes.get("topic", old["topic"]) == old["topic"]:
                    changes["topic"] = ""
            changes["updated_at"] = now_iso()
            db.execute("UPDATE memories SET " + ",".join(k + "=?" for k in changes) + " WHERE device_id=? AND id=?",
                       (*changes.values(), device, memory_id))
            current = dict(old)
            current.update(changes)
            self._index(db, device, "memory", memory_id,
                        " ".join((current["content"], current["topic"], current["keywords"])))
            db.execute("UPDATE devices SET revision=revision+1, context_revision=context_revision+1 WHERE device_id=?", (device,))
        return self.detail(device, memory_id)

    def delete(self, device, memory_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT content FROM memories WHERE device_id=? AND id=? AND status!='deleted'",
                             (device, memory_id)).fetchone()
            if not row:
                raise KeyError("记忆不存在")
            # A tombstone prevents re-extraction; transcripts remain available
            # to the owner but suppressed source messages never enter model recall.
            db.execute("UPDATE memories SET status='deleted', follow_up_at=NULL, updated_at=? WHERE id=?",
                       (now_iso(), memory_id))
            db.execute("INSERT OR IGNORE INTO blocked_memories VALUES (?, ?)", (device, fingerprint(row[0])))
            self._suppress_sources(db, device, memory_id)
            self._index(db, device, "memory", memory_id, "")
            db.execute("UPDATE devices SET revision=revision+1, context_revision=context_revision+1 WHERE device_id=?", (device,))

    def apply_extraction(self, device, batch, payload, revision):
        """Commit one model result atomically; concurrent manual edits win."""
        if not isinstance(payload, dict) or not isinstance(payload.get("memories"), list):
            raise ValueError("模型返回的记忆格式不正确")
        source_ids = {row["id"] for row in batch}
        if len(payload["memories"]) > 100:
            raise ValueError("一次最多提取100条记忆")
        prepared = []
        for item in payload["memories"]:
            if not isinstance(item, dict):
                raise ValueError("无效的记忆条目")
            content = item.get("content", "")
            sources = item.get("source_ids", [])
            if (not isinstance(content, str) or not content.strip() or len(content) > 4000
                    or not isinstance(sources, list) or not sources
                    or not all(isinstance(s, str) and s in source_ids for s in sources)):
                raise ValueError("记忆缺少有效的原文来源")
            category = item.get("category", "event")
            if category not in CATEGORIES:
                raise ValueError("无效的记忆分类")
            if not isinstance(item.get("keywords", []), list) or not all(
                    isinstance(word, str) for word in item.get("keywords", [])):
                raise ValueError("关键词必须为文字列表")
            event_time = item.get("event_time") or None
            if event_time:
                datetime.fromisoformat(event_time.replace("Z", "+00:00"))
            prepared.append((item, content.strip(), category, event_time,
                             canonical_time(item.get("follow_up_at"))))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision FROM devices WHERE device_id=?", (device,)).fetchone()
            if current and current[0] != revision:
                return False
            # Another process may have already extracted this batch while the
            # model request was running. Never apply stale state transitions.
            if source_ids and not any(db.execute("""SELECT 1 FROM messages WHERE device_id=?
                    AND id=? AND processed=0 AND excluded=0""", (device, source)).fetchone()
                    for source in source_ids):
                return True
            for item, content, category, event_time, follow_up in prepared:
                if db.execute("SELECT 1 FROM blocked_memories WHERE device_id=? AND fingerprint=?",
                              (device, fingerprint(content))).fetchone():
                    continue
                if any(not db.execute("SELECT 1 FROM messages WHERE device_id=? AND id=? AND excluded=0",
                                      (device, source)).fetchone() for source in item["source_ids"]):
                    continue
                existing = db.execute("SELECT id FROM memories WHERE device_id=? AND content=? AND status='active'",
                                      (device, content)).fetchone()
                memory_id = existing[0] if existing else str(uuid.uuid4())
                if not existing:
                    topic = str(item.get("topic", ""))[:200]
                    keywords = json.dumps(item.get("keywords", []), ensure_ascii=False)
                    created = now_iso()
                    db.execute("""INSERT INTO memories
                        (id, device_id, content, category, topic, keywords, event_time,
                         follow_up_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (memory_id, device, content, category, topic, keywords, event_time, follow_up, created, created))
                    self._index(db, device, "memory", memory_id, f"{content} {topic} {keywords}")
                for source in item["source_ids"]:
                    db.execute("INSERT OR IGNORE INTO memory_sources VALUES (?, ?)", (memory_id, source))
                supersedes = item.get("supersedes", [])
                if not isinstance(supersedes, list):
                    raise ValueError("supersedes必须是列表")
                for old_id in supersedes:
                    if old_id != memory_id:
                        db.execute("""UPDATE memories SET status='historical', follow_up_at=NULL, updated_at=?
                            WHERE device_id=? AND id=? AND status='active'""", (now_iso(), device, old_id))
            completed = payload.get("completed_follow_ups", [])
            if not isinstance(completed, list):
                raise ValueError("completed_follow_ups必须是列表")
            for memory_id in completed:
                db.execute("UPDATE memories SET follow_up_at=NULL WHERE device_id=? AND id=?", (device, memory_id))
            for source in source_ids:
                db.execute("UPDATE messages SET processed=1 WHERE device_id=? AND id=?", (device, source))
        return True

    def claim_follow_up(self, device, session, cooldown_hours=72):
        current = now_iso()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)).isoformat(timespec="microseconds")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM memories WHERE device_id=? AND status='active'
                AND follow_up_at IS NOT NULL AND follow_up_at<=?
                AND (last_follow_up_at IS NULL OR last_follow_up_at<=?)
                AND (last_follow_up_session IS NULL OR last_follow_up_session!=?)
                AND NOT EXISTS (SELECT 1 FROM memory_sources s JOIN messages t ON s.message_id=t.id
                    WHERE s.memory_id=memories.id AND t.device_id=? AND t.session_id=?)
                ORDER BY COALESCE(last_follow_up_at, ''), follow_up_at LIMIT 1""",
                (device, current, cutoff, session, device, session)).fetchone()
            if not row:
                return None
            db.execute("UPDATE memories SET last_follow_up_at=?, last_follow_up_session=? WHERE id=?",
                       (current, session, row["id"]))
            return dict(row)

    def import_summary(self, device, summary):
        if not isinstance(summary, str) or not summary.strip():
            return
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO devices(device_id) VALUES (?)", (device,))
            if db.execute("SELECT 1 FROM imports WHERE device_id=? AND name='legacy_summary'", (device,)).fetchone():
                return
            memory_id, created = str(uuid.uuid4()), now_iso()
            db.execute("""INSERT INTO memories
                (id, device_id, content, category, topic, created_at, updated_at)
                VALUES (?, ?, ?, 'profile', '旧版记忆摘要（无原文来源）', ?, ?)""",
                (memory_id, device, summary, created, created))
            self._index(db, device, "memory", memory_id, summary)
            db.execute("INSERT INTO imports VALUES (?, 'legacy_summary')", (device,))

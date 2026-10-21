"""Save spoken action recipes and replay their validated Otto steps by name."""

import asyncio
import json
import re
import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace

from core.character.stories import plain_text
from core.memory.store import database_path, now_iso


def action_teaching_request(query):
    text = re.sub(r"^(?:海绵宝宝|小智)[，,：:\s]*", "", plain_text(query).strip())
    text = text.rstrip("。！!？? \t")
    if re.fullmatch(r"(?:请)?(?:停止执行|停止动作|取消动作|取消执行)", text):
        return {"kind": "stop"}
    if re.fullmatch(r"(?:请)?(?:列出学过的动作|查看动作列表|列出动作|你(?:都)?学会了哪些动作|有哪些已保存的动作)", text):
        return {"kind": "list"}
    teach = re.fullmatch(
        r"(?:现在[，,\s]*)?(?:我(?:来|要|想)?)?(?:教你|教给你|教)(?:一个|一套|个)?动作[，,\s]*"
        r"(?:叫做|名叫|名为|叫)?\s*(.+?)[：:,，]\s*(.*)", text, re.S)
    if teach is None:
        teach = re.fullmatch(r"(?:请)?(?:记住|保存)[，,\s]*(?:一个|一套|这个)?动作[，,\s]*(?:叫做|名叫|名为|叫)?\s*(.+?)(?:的步骤)?(?:是|为|[：:,，])\s*(.*)", text, re.S)
    if teach is None:
        teach = re.fullmatch(r"(?:把)?动作\s*(.+?)(?:的步骤)?(?:改成|改为|修改为|更新为)\s*(.*)", text, re.S)
    if teach is not None:
        return {"kind": "teach", "name": teach.group(1), "script": teach.group(2)}
    for kind, pattern in (
        ("execute", r"(?:请)?(?:执行|做一下|做一遍)(?:一下|一遍)?(?:动作)?\s*(.+)"),
        ("show", r"(?:请)?(?:复述|查看)(?:一下)?动作\s*(.+)"),
        ("delete", r"(?:请)?(?:删除|忘掉)动作\s*(.+)"),
    ):
        match = re.fullmatch(pattern, text)
        if match:
            return {"kind": kind, "name": match.group(1).removesuffix("吧") if kind == "execute" else match.group(1)}
    if re.match(r"^(?:(?:我(?:来|要|想)?)?(?:教你|教给你|教)(?:一个|一套|个)?动作|(?:记住|保存)(?:一个|一套|这个)?动作)", text):
        return {"kind": "help"}
    return None


def action_name(value):
    name = value.strip().strip('“”"「」『』').strip()
    if not 1 <= len(name) <= 30 or re.search(r"[，,。！？?!；;：:\n]|然后|假装|故事|翻译", name):
        raise ValueError("动作名称请用三十字以内的简短名称，不要包含多条指令。")
    if name in {"停下", "停止", "停止动作", "停止执行"}:
        raise ValueError("这个名称用于停止控制，请换一个动作名称。")
    return name


def _number(value):
    if not value:
        return 1
    if value.isascii() and value.isdigit():
        return int(value)
    digits = {ch: index for index, ch in enumerate("零一二三四五六七八九")}
    value = value.replace("两", "二")
    if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", value):
        tens, ones = value.split("十")
        return (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)
    if value in digits:
        return digits[value]
    raise ValueError("动作次数请使用明确的整数。")


def parse_steps(script):
    chunks = re.split(r"[，,、；;。\n]+|然后|接着|最后|再|并且", script.strip())
    steps = []
    numeral = r"([0-9零一二三四五六七八九十两]+)?"
    movements = (
        (r"(?:前进|向前走|往前走|往前|向前)", "walk", 1),
        (r"(?:后退|向后走|往后走|往后|向后)", "walk", -1),
        (r"(?:左转|向左转|往左转)", "turn", 1),
        (r"(?:右转|向右转|往右转)", "turn", -1),
        (r"(?:挥手|挥一下手)", "hand_wave", 1),
        (r"(?:举手|举起双手|举起手)", "hands_up", 1),
        (r"(?:放下双手|放下手|放手)", "hands_down", 1),
        (r"(?:坐下)", "sit", 1),
        (r"(?:站起来|站起|复位)", "home", 1),
    )
    for chunk in chunks:
        text = re.sub(r"^(?:先|请|再|接着|最后)\s*", "", chunk.strip())
        if not text:
            continue
        if re.fullmatch(r"停下|停止|停住", text):
            steps.append({"action": "stop", "count": 1})
            continue
        match = re.fullmatch(r"(?:等待|等|暂停)" + numeral + r"秒(?:钟)?", text)
        if match:
            count = _number(match.group(1))
            if not 1 <= count <= 10:
                raise ValueError("每次等待请设为一到十秒。")
            steps.append({"action": "wait", "count": count})
            continue
        for phrase, action, direction in movements:
            match = re.fullmatch(phrase + numeral + r"(?:小步|步|次|下)?", text)
            if match:
                count = _number(match.group(1))
                if not 1 <= count <= 10:
                    raise ValueError("每项动作请设为一到十步或次。")
                steps.append({"action": action, "direction": direction, "count": count})
                break
        else:
            raise ValueError(f"没有理解“{text[:30]}”。请用前进、后退、左右转、挥手、举手、放下手、坐下、站起来、等待或停下，并用步、次、秒说明数量。")
    if not steps or len(steps) > 12:
        raise ValueError("请一次教一到十二项动作。")
    if sum(s["count"] for s in steps if s["action"] not in {"wait", "stop"}) > 30:
        raise ValueError("一套动作最多三十步或次，请拆成多个动作。")
    if sum(s["count"] for s in steps if s["action"] == "wait") > 30:
        raise ValueError("一套动作的等待时间合计最多三十秒。")
    return steps


def describe_steps(steps):
    phrases = {("walk", 1): "前进", ("walk", -1): "后退", ("turn", 1): "左转", ("turn", -1): "右转",
               ("hand_wave", 1): "挥手", ("hands_up", 1): "举手", ("hands_down", 1): "放下手",
               ("sit", 1): "坐下", ("home", 1): "站起来"}
    parts = []
    for step in steps:
        action, count = step["action"], step["count"]
        if action == "wait":
            parts.append(f"等待{count}秒")
        elif action == "stop":
            parts.append("停下")
        else:
            parts.append(phrases[(action, step["direction"])] + str(count) + ("步" if action in {"walk", "turn"} else "次"))
    return "，再".join(parts)


class ActionLibrary:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS learned_actions (
                device_id TEXT NOT NULL, name TEXT NOT NULL, steps_json TEXT NOT NULL,
                source_text TEXT NOT NULL, source_message_id TEXT, updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (device_id, name))""")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def save(self, device, name, steps, source, message_id):
        # Validate the canonical representation as well as the spoken script.
        parse_steps(describe_steps(steps))
        with self.connect() as db:
            db.execute("""INSERT INTO learned_actions
                (device_id,name,steps_json,source_text,source_message_id,updated_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(device_id,name) DO UPDATE SET
                steps_json=excluded.steps_json,source_text=excluded.source_text,
                source_message_id=excluded.source_message_id,updated_at=excluded.updated_at,
                revision=learned_actions.revision+1,deleted=0""",
                (device, name, json.dumps(steps, ensure_ascii=False), source, message_id, now_iso()))

    def get(self, device, name):
        with self.connect() as db:
            row = db.execute("SELECT steps_json FROM learned_actions WHERE device_id=? AND name=? AND deleted=0", (device, name)).fetchone()
        if row is None:
            return None
        # Revalidate on loading, so stale or manually corrupted records cannot
        # inject arbitrary MCP functions or extend execution limits.
        return parse_steps(describe_steps(json.loads(row[0])))

    def names(self, device):
        with self.connect() as db:
            return [r[0] for r in db.execute("SELECT name FROM learned_actions WHERE device_id=? AND deleted=0 ORDER BY updated_at DESC LIMIT 30", (device,))]

    def delete(self, device, name):
        with self.connect() as db:
            return db.execute("UPDATE learned_actions SET deleted=1,revision=revision+1,updated_at=? WHERE device_id=? AND name=? AND deleted=0", (now_iso(), device, name)).rowcount > 0


async def replay_action(conn, name, steps, sentence_id, report_tool_call):
    from .medicine_navigation import result_value, stop_navigation

    # Navigation, learned actions, and the existing abort handler share one
    # serialized motion session; a replacement action stops the previous one.
    await stop_navigation(conn)
    lock = getattr(conn, "_medicine_navigation_lock", None)
    if lock is None:
        lock = conn._medicine_navigation_lock = asyncio.Lock()
    async with lock:
        state = SimpleNamespace(task=asyncio.current_task(), cancelled=False, started=False)
        conn._medicine_navigation = state

        def current():
            return not state.cancelled and not conn.client_abort and conn.sentence_id == sentence_id

        async def call(tool, arguments):
            if not current():
                raise asyncio.CancelledError()
            data = {"id": uuid.uuid4().hex, "name": tool, "arguments": json.dumps(arguments, ensure_ascii=False)}
            report_tool_call(conn, tool, arguments)
            result = await asyncio.wait_for(conn.func_handler.handle_llm_function_call(conn, data), 4)
            if not current():
                raise asyncio.CancelledError()
            value = result_value(result)
            report_tool_call(conn, tool, arguments, str(value), report_tool_call=False)
            from core.utils.dialogue import Message
            conn.dialogue.put(Message(role="assistant", tool_calls=[{"id": data["id"], "type": "function",
                "function": {"name": tool, "arguments": data["arguments"]}}]))
            conn.dialogue.put(Message(role="tool", tool_call_id=data["id"], content=str(value)))
            if tool != "self_otto_get_status" and str(value).lower() not in {"true", "1"}:
                raise ValueError("Device did not acknowledge action")
            return value

        async def idle():
            await asyncio.sleep(1.4)
            for _ in range(12):
                if str(await call("self_otto_get_status", {})).strip('" \n') == "idle":
                    return
                await asyncio.sleep(.25)
            raise ValueError("Action did not finish")

        answer = f"动作“{name}”已执行完毕。"
        try:
            steps = parse_steps(describe_steps(steps))
            if not all(conn.func_handler.has_tool(tool) for tool in ("self_otto_action", "self_otto_stop", "self_otto_get_status")):
                return "动作工具尚未接通，这次没有执行，请等机器人连接完成后再试。"
            if not current():
                raise asyncio.CancelledError()
            state.started = True
            await call("self_otto_stop", {})
            await idle()
            for step in steps:
                if step["action"] == "wait":
                    await asyncio.sleep(step["count"])
                elif step["action"] == "stop":
                    await call("self_otto_stop", {})
                    await idle()
                else:
                    for _ in range(step["count"]):
                        await call("self_otto_action", {"action": step["action"], "steps": 1, "speed": 1000,
                            "direction": step["direction"], "amount": 30, "arm_swing": 0})
                        await idle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            conn.logger.warning(f"已学动作执行中止: {type(exc).__name__}")
            answer = f"动作“{name}”没有执行完成，我已中止后续步骤。"
        finally:
            if state.started:
                try:
                    result = await asyncio.wait_for(conn.func_handler.handle_llm_function_call(conn,
                        {"id": uuid.uuid4().hex, "name": "self_otto_stop", "arguments": "{}"}), 4)
                    if str(result_value(result)).lower() not in {"true", "1"}:
                        raise ValueError("Stop not acknowledged")
                except Exception as exc:
                    conn.logger.error(f"已学动作停止未确认: {type(exc).__name__}")
                    answer = "动作执行已结束，但设备没有确认停止，请立即检查机器人。"
            if getattr(conn, "_medicine_navigation", None) is state:
                conn._medicine_navigation = None
        return answer


def dispatch_action_command(conn, request, sentence_id, user_message, report_tool_call):
    from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
    from core.utils.dialogue import Message

    response = None
    try:
        device = getattr(conn, "device_id", None) or getattr(conn, "headers", {}).get("device-id")
        if not device:
            raise ValueError("尚未确认设备身份，暂时不能保存或执行动作。")
        kind = request["kind"]
        if kind == "help":
            response = "请一次说完整，例如：教你一个动作叫拿感冒药，先左转一步，再前进两步，最后停下。"
        elif kind == "stop":
            from .medicine_navigation import dispatch_medicine_request
            return dispatch_medicine_request(conn, {"stop": True}, sentence_id, report_tool_call)
        else:
            library = ActionLibrary(database_path(conn.config.get("local_memory", {})))
            if kind == "list":
                names = library.names(device)
                response = "我学会的动作有：" + "、".join(names) + "。" if names else "还没有保存动作，你可以先教我一套步骤。"
            else:
                name = action_name(request["name"])
                if kind == "teach":
                    steps = parse_steps(request["script"])
                    library.save(device, name, steps, plain_text(user_message.content), user_message.uniq_id)
                    response = f"已保存动作“{name}”：{describe_steps(steps)}。之后说“执行{name}”就可以。"
                elif kind == "delete":
                    response = f"已删除动作“{name}”。" if library.delete(device, name) else f"没有找到动作“{name}”。"
                else:
                    steps = library.get(device, name)
                    if steps is None:
                        response = f"我还没有学过动作“{name}”，请先教我完整步骤。"
                    elif kind == "show":
                        response = f"动作“{name}”的步骤是：{describe_steps(steps)}。"
                    elif kind == "execute":
                        pending = asyncio.run_coroutine_threadsafe(
                            asyncio.wait_for(replay_action(conn, name, steps, sentence_id, report_tool_call), 120), conn.loop)
                        try:
                            response = pending.result(timeout=126)
                        except Exception:
                            pending.cancel()
                            response = f"动作“{name}”执行已结束，未确认全部步骤完成。"
    except ValueError as exc:
        response = str(exc)
    except Exception as exc:
        conn.logger.warning(f"动作教学失败: {type(exc).__name__}")
        response = "这次动作保存或读取失败，请稍后重试。"
    # These are executable recipes, not unstructured personal facts.
    memory_store = getattr(getattr(conn, "memory", None), "store", None)
    if device and memory_store is not None and hasattr(memory_store, "exclude_message"):
        try:
            memory_store.exclude_message(device, user_message.uniq_id)
        except Exception as exc:
            conn.logger.warning(f"动作消息归档标记失败: {type(exc).__name__}")
    if conn.client_abort or conn.sentence_id != sentence_id:
        return False
    if response:
        conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=sentence_id,
            sentence_type=SentenceType.MIDDLE, content_type=ContentType.TEXT, content_detail=response))
        conn.tts.store_tts_text(sentence_id, response)
        conn.dialogue.put(Message(role="assistant", content=response))
    conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=sentence_id,
        sentence_type=SentenceType.LAST, content_type=ContentType.ACTION))
    return True

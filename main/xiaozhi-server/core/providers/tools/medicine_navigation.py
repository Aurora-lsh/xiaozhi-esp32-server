"""Bounded, camera-in-the-loop approach to a named medicine using Otto MCP."""

import asyncio
import json
import math
import re
import uuid
from types import SimpleNamespace
from urllib.parse import quote

from core.character.stories import plain_text


def medicine_request(query):
    text = plain_text(query).strip()
    text = re.sub(r"^(?:海绵宝宝|小智)[，,：:\s]*", "", text)
    if re.fullmatch(r"(?:请|你)?(?:停|停下|停止|停止寻药|停止找药|别走了|别动了|不要动)[吧了。！!\s]*", text):
        return {"stop": True}
    if re.search(r"不要|不用|别|不许|禁止|假装|故事|翻译|怎么|如何|昨天|刚才|如果|能否", text):
        return None
    prefix = r"(?:(?:请|你|现在|先|帮我|给我|麻烦你|麻烦|能不能|可以)[，,\s]*)*"
    match = re.fullmatch(prefix + r"(?:拿|取|找|寻找)(?:(?:一下|一盒|一瓶|一包|一点|一些|点|个))*([^\n]+?)(?:过来|给我)?[吧呀啊。！!？?\s]*", text)
    if match is None:
        match = re.fullmatch(prefix + r"(?:走|移动|去|到)(?:到)?(.+?)(?:的)?(?:前面|面前|旁边)[吧呀啊。！!？?\s]*", text)
    if match is None:
        return None
    target = match.group(1).strip(" ，,。.!！?？")
    # Standalone fragments such as “感冒药的面前” are not movement commands.
    if not re.search(r"药|布洛芬|阿莫西林|维生素|板蓝根|阿司匹林|对乙酰氨基酚|感冒灵|连花清瘟", target):
        return None
    if len(target) > 30 or re.search(r"[，,。！？?!；;\n]|然后|顺便|什么|哪种|多少|颜色|能不能|可以吗", target):
        return None
    return {"target": target}


def medicine_identification_request(query, options):
    """Identify a configured demo medicine without interpreting it as movement."""
    text = re.sub(r"^(?:海绵宝宝|小智)[，,：:\s]*", "", plain_text(query).strip())
    if re.search(r"故事|剧本|假装|想象|台词|翻译|昨天|刚才|如果|不要|不用|别(?:再|看|帮|找|识别|辨认|拍|动|走|指)|不许|禁止|怎么|如何|副作用|剂量|服用", text):
        return None
    targets = options.get("targets", {})
    mentioned = [name for name in targets if name in text]
    if len(mentioned) != 1:
        return None
    # Restrict this route to identification/location wording, not general
    # medication questions or a bare mention of a medicine.
    if not re.search(r"哪(?:一)?(?:瓶|罐|个|里|边|儿)|什么颜色|在[左右中]|看看|看一下|识别|辨认|认一下|认一认|指一下", text):
        return None
    observe = bool(re.search(r"面前|前面|眼前|这|那|哪|看|识别|辨认|认一下|认一认|指一下|在[左右中]", text))
    return {"target": mentioned[0], "identify": True, "observe": observe}


def identification_prompt(target, identity_mode="marker"):
    identity = "label" if identity_mode == "label" else "target"
    prompt = (f"Find {target}. Only JSON [match,x]. "
              f"match=1 if unique visible {identity},0 if absent,null if unsure. x=center/W 0..1.")
    if len(quote(prompt, safe="")) > 255:
        raise ValueError("识别目标名称超过摄像头提示词长度限制")
    return "NAV1 " + prompt


def navigation_prompt(target, identity_mode="label"):
    # NAV1 bypasses the camera bridge's prose wrapper. Never silently truncate
    # either the medicine name or the coordinate contract at the firmware limit.
    identity = "label" if identity_mode == "label" else "marker"
    prompt = (f"Find {target}. JSON [match,x,h,clear]. "
              "x=center/W h=height/H 0..1. "
              f"match=unique {identity} 0/1. clear=safe visible floor 0/1. unknown=null.")
    if len(quote(prompt, safe="")) > 255:
        raise ValueError("药名太长，请使用药盒上的简短名称再试。")
    return "NAV1 " + prompt


def _camera_data(payload):
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("摄像头没有成功返回本次画面。")
    text = payload.get("description", "")
    if not isinstance(text, str):
        raise ValueError("摄像头返回的位置格式不完整，已停止。")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


def identification_position(payload):
    data = _camera_data(payload)
    # The live vision service may name the requested fields instead of returning
    # an array. Apply the same strict value checks to both JSON representations.
    if isinstance(data, dict):
        data = [data.get("match"), data.get("x")]
    if not isinstance(data, list) or len(data) != 2 or type(data[0]) is not int or data[0] not in (0, 1):
        raise ValueError("没有明确的目标识别结果")
    match, x = data
    if not match:
        return None
    if type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1:
        raise ValueError("目标位置无法确认")
    return "左侧" if x < 0.35 else "右侧" if x > 0.65 else "中间"


def observation(payload):
    """Accept only the explicit vision contract; never infer motion from prose."""
    data = _camera_data(payload)
    if isinstance(data, dict):
        data = [data.get(key) for key in ("match", "x", "h", "clear")]
    if not isinstance(data, list) or len(data) != 4:
        raise ValueError("摄像头没有返回明确位置，已停止。")
    match, x, height, clear = data
    if type(match) is not int or match not in (0, 1) or type(clear) is not int or clear not in (0, 1):
        raise ValueError("药名或前方地面无法确认，已停止。")
    if match:
        for value in (x, height):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("药品位置无法确认，已停止。")
        if height == 0:
            raise ValueError("药品大小无法确认，已停止。")
    return {"match": bool(match), "x": x, "height": height, "clear": bool(clear)}


def result_value(result):
    from plugins_func.register import Action
    if result is None or result.action in (Action.ERROR, Action.NOTFOUND):
        raise ValueError("工具连接异常，已停止本次寻药。")
    return result.result if result.result is not None else result.response


async def stop_navigation(conn):
    """Abort only an active navigation; ordinary chat never moves the robot."""
    state = getattr(conn, "_medicine_navigation", None)
    if state is not None:
        if state.task is asyncio.current_task():
            state.cancelled = True
            return
        if not state.cancelled:
            state.cancelled = True
            state.task.cancel()
        # The task's finally block owns stop, including cancellation in a call.
        try:
            await asyncio.wait_for(asyncio.shield(state.task), timeout=4)
        except (asyncio.CancelledError, Exception):
            pass


async def navigate(conn, request, sentence_id, report_tool_call):
    from .camera_routing import prepare_camera_request

    old = getattr(conn, "_medicine_navigation", None)
    if old is not None:
        await stop_navigation(conn)
    lock = getattr(conn, "_medicine_navigation_lock", None)
    if lock is None:
        lock = conn._medicine_navigation_lock = asyncio.Lock()
    async with lock:
        if request.get("stop"):
            if not conn.func_handler.has_tool("self_otto_stop"):
                return "停止工具没有接通，请检查机器人。"
            try:
                result = await asyncio.wait_for(conn.func_handler.handle_llm_function_call(conn,
                    {"id": uuid.uuid4().hex, "name": "self_otto_stop", "arguments": "{}"}), 4)
                if str(result_value(result)).lower() not in ("true", "1"):
                    raise ValueError("Stop not acknowledged")
                return "已发送停止指令。"
            except Exception:
                return "设备没有确认停止，请立即检查机器人。"
        state = SimpleNamespace(task=asyncio.current_task(), cancelled=False, started=False)
        conn._medicine_navigation = state
        options = conn.config.get("medicine_navigation", {})

        def current():
            return not state.cancelled and not conn.client_abort and conn.sentence_id == sentence_id

        async def call(name, arguments, timeout):
            if not current():
                raise asyncio.CancelledError()
            data = {"id": uuid.uuid4().hex, "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False)}
            report_tool_call(conn, name, arguments)
            result = await asyncio.wait_for(conn.func_handler.handle_llm_function_call(conn, data), timeout)
            if not current():
                raise asyncio.CancelledError()
            value = result_value(result)
            report_tool_call(conn, name, arguments, str(value), report_tool_call=False)
            # Retain actual observations/actions, without asking the chat model
            # to reinterpret navigation results or issue additional movements.
            from core.utils.dialogue import Message
            conn.dialogue.put(Message(role="assistant", tool_calls=[{"id": data["id"],
                "type": "function", "function": {"name": name, "arguments": data["arguments"]}}]))
            conn.dialogue.put(Message(role="tool", tool_call_id=data["id"], content=str(value)))
            return value

        async def idle():
            # Otto acknowledges queue insertion, not completion. Allow the worker
            # to start and require idle after the nominal single-step period.
            await asyncio.sleep(1.4)
            for _ in range(12):
                value = await call("self_otto_get_status", {}, 4)
                if str(value).strip('" \n') == "idle":
                    return
                await asyncio.sleep(0.25)
            raise ValueError("机器人动作未按时结束，已停止。")

        async def move(action, direction):
            value = await call("self_otto_action", {"action": action, "steps": 1,
                "speed": 1000, "direction": direction, "arm_swing": 0}, 5)
            if str(value).lower() not in ("true", "1"):
                raise ValueError("Movement not acknowledged")
            await idle()

        answer = None
        try:
            if options.get("enabled", True) is not True and not request.get("identify"):
                return "寻药移动功能尚未开启。"
            target = request["target"]
            identity_mode = options.get("identity_mode", "label")
            markers = options.get("targets", {})
            if identity_mode not in ("label", "marker"):
                raise ValueError("Unknown medicine identity mode")
            if identity_mode == "marker" and not markers.get(target):
                return f"还没有配置{target}对应的颜色或标签，请先设置对应关系。"
            visual_target = markers.get(target, target)
            if not isinstance(visual_target, str) or not visual_target.strip():
                raise ValueError("Invalid visual target")
            if request.get("identify") and not request.get("observe", True) and identity_mode == "marker":
                return f"按我们的演示约定，{visual_target}代表{target}。"
            prompt = (identification_prompt(visual_target, identity_mode) if request.get("identify")
                      else navigation_prompt(visual_target, identity_mode))
            await prepare_camera_request(conn, {"kind": "capability"})
            conn.func_handler.tool_manager.refresh_tools()
            if request.get("identify"):
                if not conn.func_handler.has_tool("look_at_camera"):
                    return "摄像头工具没有接通，我现在还不能确认面前哪一个是目标。"
                location = identification_position(await call("look_at_camera", {"question": prompt}, 110))
                if location is None:
                    return (f"按约定，{visual_target}代表{target}，但这次画面中没有确认到它。" if identity_mode == "marker"
                            else f"这次画面中没有确认到{target}。")
                return (f"{visual_target}代表{target}，我在画面{location}看到了它。" if identity_mode == "marker"
                        else f"我在画面{location}看到了{target}。")
            if not all(conn.func_handler.has_tool(name) for name in
                       ("look_at_camera", "self_otto_action", "self_otto_stop", "self_otto_get_status")):
                return "摄像头或行走工具尚未接通，这次不能移动到药品前面。"
            if not current():
                raise asyncio.CancelledError()
            state.started = True
            value = await call("self_otto_stop", {}, 4)
            if str(value).lower() not in ("true", "1"):
                raise ValueError("Stop not acknowledged")
            await idle()
            max_observations = min(20, max(2, int(options.get("max_observations", 12))))
            threshold = float(options.get("stop_height_ratio", 0.45))
            if not math.isfinite(threshold) or not 0.15 <= threshold <= 0.8:
                raise ValueError("寻药停止阈值配置无效。")
            search_turns = forward_steps = near_frames = 0
            seen = False
            for index in range(max_observations):
                view = observation(await call("look_at_camera", {"question": prompt}, 110))
                conn.logger.info(f"寻药定位: target={target}, sample={index + 1}, observation={view}")
                if not view["clear"]:
                    answer = "前方地面或通路无法确认，我已经停下。"
                    break
                if not view["match"]:
                    if seen:
                        answer = f"暂时看不到{target}了，我已经停下，请把药盒标签朝向摄像头。"
                        break
                    if search_turns >= 4:
                        answer = f"附近没有找到能确认名称的{target}，我已经停下。"
                        break
                    search_turns += 1
                    await move("turn", 1)
                    continue
                seen = True
                x, height = view["x"], view["height"]
                if height >= threshold:
                    # Do not turn/walk when already close. Require a second fresh
                    # centered view before reporting a visual approach success.
                    if not 0.35 <= x <= 0.65:
                        answer = f"{target}已经很近，但不在正前方，我先停下。"
                        break
                    near_frames += 1
                    if near_frames >= 2:
                        answer = f"我看到{target}就在前方，已经停下。"
                        break
                    continue
                near_frames = 0
                if x < 0.35 or x > 0.65:
                    await move("turn", 1 if x < 0.35 else -1)
                else:
                    if forward_steps >= 6:
                        answer = f"这次还没有接近{target}，已到行走步数上限，我先停下。"
                        break
                    forward_steps += 1
                    await move("walk", 1)
            if answer is None:
                answer = f"本次寻找{target}已到观察次数上限，我先停下。"
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError) as exc:
            conn.logger.warning(f"寻药停止: {type(exc).__name__}: {str(exc)[:160]}")
            answer = ("这次画面没有清楚确认目标，我还不能判断是哪一个，请把目标放到摄像头能看清的位置再试。"
                      if request.get("identify") else
                      "这次没有确认目标或位置，我已经停止寻药，请把目标放到摄像头能看清的位置再试。")
        except Exception as exc:
            conn.logger.warning(f"寻药工具异常: {type(exc).__name__}")
            answer = ("摄像头连接异常，这次没有确认到面前的物体。" if request.get("identify")
                      else "摄像头或行走连接异常，我已结束本次寻药。")
        finally:
            if state.started:
                # Stop must also run after generation invalidation or timeout.
                try:
                    result = await asyncio.wait_for(conn.func_handler.handle_llm_function_call(conn,
                        {"id": uuid.uuid4().hex, "name": "self_otto_stop", "arguments": "{}"}), 4)
                    value = result_value(result)
                    if str(value).lower() not in ("true", "1"):
                        raise ValueError("Stop not acknowledged")
                except Exception as exc:
                    conn.logger.error(f"寻药停止指令未确认: {type(exc).__name__}")
                    answer = "本次寻药已结束，但设备没有确认停止，请立即检查机器人。"
            if getattr(conn, "_medicine_navigation", None) is state:
                conn._medicine_navigation = None
        return answer


def dispatch_medicine_request(conn, request, sentence_id, report_tool_call):
    """Bridge the synchronous chat worker to a cancellable async controller."""
    from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
    from core.utils.dialogue import Message

    pending = asyncio.run_coroutine_threadsafe(navigate(conn, request, sentence_id, report_tool_call), conn.loop)
    try:
        # Each iteration is bounded; also enforce an overall wall-clock budget.
        response = pending.result(timeout=240)
    except Exception:
        pending.cancel()
        response = "本次寻药已经结束。"
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

"""Route explicit, present-tense camera requests to real available tools."""
import asyncio
import json
import re
import uuid

from core.character.stories import plain_text


# ASR commonly uses either 张 or 个; accept both while keeping non-camera
# actions such as 拍手/拍球/拍电影 outside the direct camera route.
_PHOTO_ACTION = r"(?:拍(?:[一1]?(?:张|个)|一下)?(?:照(?:片)?|相)|拍[一1]?张|照(?:[一1]?(?:张|个))?相)"


def camera_request(query):
    text = plain_text(query).strip()
    text = re.sub(r"^(?:海绵宝宝|小智)[，,：:\s]*", "", text)
    if not text or re.search(r"故事|剧本|假装|想象|台词|翻译|怎么|如何|不要|别再|别拍|不用拍|不许|禁止", text):
        return None
    if re.fullmatch(r"(?:你|现在|你现在)*(?:有|能|可以)(?:拍照|看摄像头|使用摄像头)(?:的)?(?:功能|能力)?[吗么？?。！!\s]*", text):
        return {"kind": "capability", "question": text}
    prefix = r"(?:(?:你|请|再|先|现在|帮我|给我|麻烦|能不能|可以|一下|用摄像头|打开摄像头)[，,\s]*)*"
    if re.match(r"^" + prefix + r"(?:连续(?:拍|看|观察)|观察(?:一下)?(?:前面|面前|周围|摄像头)|盯着(?:看|摄像头))", text):
        duration = re.search(r"(\d+)\s*秒", text)
        return {"kind": "watch", "question": text,
                "duration_seconds": min(10, max(1, int(duration.group(1)))) if duration else 3}
    if re.match(r"^" + prefix + r"(?:" + _PHOTO_ACTION + r"|看看(?:我|你)?(?:面前|前面|眼前|摄像头|镜头|周围|桌上|手里)|看(?:一下)?(?:摄像头|镜头))", text):
        return {"kind": "snapshot", "question": text}
    return None


async def prepare_camera_request(conn, request):
    client = getattr(conn, "mcp_endpoint_client", None)
    from .mcp_endpoint.mcp_endpoint_handler import connect_mcp_endpoint, refresh_mcp_endpoint_tools
    endpoint = conn.config.get("mcp_endpoint")
    if endpoint and (client is None or not await client.is_ready()):
        if client is not None:
            await client.close()
        client = await connect_mcp_endpoint(endpoint, conn)
        conn.mcp_endpoint_client = client
        if client is None:
            raise ConnectionError("Camera MCP endpoint unavailable")
    if client is not None:
        # The camera bridge may have joined after this conversation started.
        # Refresh only for explicit camera requests, without slowing normal chat.
        await refresh_mcp_endpoint_tools(client, timeout=6)
    available = conn.func_handler.has_tool("look_at_camera")
    if request["kind"] == "capability":
        return {"response": "我现在可以通过摄像头拍照，看看面前的物体和环境。" if available
                else "摄像头工具目前没有接入，我现在还不能拍照。"}
    name = "watch_camera" if request["kind"] == "watch" else "look_at_camera"
    if not conn.func_handler.has_tool(name):
        return {"response": "摄像头工具目前没有接入，这次没有拍到照片，请检查摄像头的 MCP 连接。"}
    arguments = {"question": request["question"]}
    if name == "watch_camera":
        arguments["duration_seconds"] = request["duration_seconds"]
    return {"name": name, "arguments": arguments}


def dispatch_camera_request(conn, request, sentence_id, report_tool_call):
    """Use the normal tool executor/report/history/TTS path, not a spoken promise."""
    from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
    from core.utils.dialogue import Message

    def current():
        return not conn.client_abort and conn.sentence_id == sentence_id

    def say(text):
        conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=sentence_id,
            sentence_type=SentenceType.MIDDLE, content_type=ContentType.TEXT, content_detail=text))
        conn.tts.store_tts_text(sentence_id, text)
        conn.dialogue.put(Message(role="assistant", content=text))

    pending = asyncio.run_coroutine_threadsafe(prepare_camera_request(conn, request), conn.loop)
    try:
        plan = pending.result(timeout=8)
    except Exception as exc:
        pending.cancel()
        conn.logger.warning(f"摄像头工具列表刷新失败: {type(exc).__name__}")
        plan = {"response": "摄像头连接暂时不可用，这次没有拍到照片，请稍后再试。"}
    if not current():
        return False
    if "response" in plan:
        say(plan["response"])
    else:
        call = {"id": uuid.uuid4().hex, "name": plan["name"],
                "arguments": json.dumps(plan["arguments"], ensure_ascii=False)}
        report_tool_call(conn, call["name"], plan["arguments"])
        pending = asyncio.run_coroutine_threadsafe(
            conn.func_handler.handle_llm_function_call(conn, call), conn.loop)
        try:
            result = pending.result(timeout=int(conn.config.get("tool_call_timeout", 30)))
        except Exception as exc:
            pending.cancel()
            conn.logger.warning(f"摄像头调用失败: {type(exc).__name__}")
            result = None
        if not current():
            return False
        if result is None:
            say("摄像头这次没有成功返回画面，我还不能判断前面有什么，请稍后再试。")
        else:
            report_tool_call(conn, call["name"], plan["arguments"],
                             str(result.result or result.response or ""), report_tool_call=False)
            conn._handle_function_result([(result, call)], depth=0)
    if current():
        conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=sentence_id,
            sentence_type=SentenceType.LAST, content_type=ContentType.ACTION))
    return current()

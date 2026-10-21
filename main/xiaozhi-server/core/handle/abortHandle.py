import json
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def handleAbortMessage(conn: "ConnectionHandler"):
    conn.logger.bind(tag=TAG).info("Abort message received")
    # 设置成打断状态，会自动打断llm、tts任务
    conn.close_after_chat = False
    conn.client_abort = True
    # A new turn may clear client_abort before an old worker unwinds. Invalidate
    # its generation as well so late text/audio can never enter the next turn.
    conn.sentence_id = str(uuid.uuid4().hex)
    from core.providers.tools.medicine_navigation import stop_navigation
    await stop_navigation(conn)
    if conn.tts is not None and hasattr(conn.tts, "cancel_synthesis"):
        conn.tts.cancel_synthesis()
    if conn.llm is not None and hasattr(conn.llm, "cancel_response"):
        conn.llm.cancel_response(conn.session_id)
    conn.clear_queues()
    # 打断客户端说话状态
    await conn.websocket.send(
        json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
    )
    conn.clearSpeakStatus()
    conn.logger.bind(tag=TAG).info("Abort message received-end")

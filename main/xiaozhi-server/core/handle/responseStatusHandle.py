"""Optional display hints, negotiated independently of the audio state machine."""
import json


async def send_response_status(conn, state):
    features = getattr(conn, 'features', None)
    if not isinstance(features, dict) or features.get('response_status') is not True:
        return
    stamp = (conn.sentence_id, state)
    if getattr(conn, '_response_status_stamp', None) == stamp:
        return
    await conn.websocket.send(json.dumps({'type':'response_status', 'state':state, 'session_id':conn.session_id}))
    conn._response_status_stamp = stamp

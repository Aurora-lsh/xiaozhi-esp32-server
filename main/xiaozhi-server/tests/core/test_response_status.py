import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.handle.responseStatusHandle import send_response_status


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_clients_receive_no_new_message(self):
        for features in (None, {}, {'response_status':False}, {'response_status':'true'}):
            conn=SimpleNamespace(features=features, websocket=SimpleNamespace(send=AsyncMock()))
            await send_response_status(conn, 'thinking')
            conn.websocket.send.assert_not_called()

    async def test_negotiated_states_follow_each_generation_once(self):
        conn=SimpleNamespace(features={'response_status':True}, sentence_id='first', session_id='session',
                             websocket=SimpleNamespace(send=AsyncMock()))
        await send_response_status(conn, 'thinking')
        await send_response_status(conn, 'thinking')
        await send_response_status(conn, 'speaking')
        await send_response_status(conn, 'speaking')
        conn.sentence_id='second'
        await send_response_status(conn, 'thinking')
        states=[json.loads(call.args[0])['state'] for call in conn.websocket.send.call_args_list]
        self.assertEqual(['thinking','speaking','thinking'], states)

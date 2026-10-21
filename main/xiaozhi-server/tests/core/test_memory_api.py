"""Exercise the real aiohttp routes against an isolated local database."""

import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.api.memory_handler import MemoryHandler
from core.utils.dialogue import Message


class MemoryAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.handler = MemoryHandler({"local_memory": {
            "database_path": str(Path(self.temp.name) / "memory.sqlite3")}})
        self.message = Message("user", "我的猫叫奶茶")
        self.handler.store.record_message("one", "first", self.message)
        self.memory = self.handler.store.remember("one", self.message.uniq_id, "用户的猫叫奶茶")
        other = Message("user", "我的猫叫布丁")
        self.handler.store.record_message("two", "first", other)
        self.other = self.handler.store.remember("two", other.uniq_id, "用户的猫叫布丁")
        app = web.Application()
        self.handler.register(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_page_static_assets_and_content_security_policy(self):
        page = await self.client.get("/memory/")
        self.assertEqual(200, page.status)
        self.assertIn("长期记忆", await page.text())
        self.assertIn("frame-ancestors 'none'", page.headers["Content-Security-Policy"])
        script = await self.client.get("/memory/app.js")
        self.assertEqual(200, script.status)
        self.assertIn("textContent", await script.text())

    async def test_list_search_and_sources_are_scoped_to_selected_device(self):
        devices = await (await self.client.get("/memory/api/devices")).json()
        self.assertEqual(2, len(devices["data"]))
        listed = await (await self.client.get("/memory/api/memories?device_id=one&q=奶茶")).json()
        self.assertEqual(1, listed["data"]["total"])
        detail = await (await self.client.get(f"/memory/api/memories/{self.memory['id']}?device_id=one")).json()
        self.assertEqual(self.message.uniq_id, detail["data"]["sources"][0]["id"])
        foreign = await self.client.get(f"/memory/api/memories/{self.other['id']}?device_id=one")
        self.assertEqual(404, foreign.status)

    async def test_edit_delete_and_archive_retention(self):
        path = f"/memory/api/memories/{self.memory['id']}?device_id=one"
        edited = await self.client.patch(path, json={"content": "用户的猫叫年糕"})
        self.assertEqual(200, edited.status)
        data = (await edited.json())["data"]
        self.assertEqual("用户的猫叫奶茶", data["revisions"][0]["content"])
        self.assertEqual(200, (await self.client.delete(path)).status)
        self.assertEqual(404, (await self.client.get(path)).status)
        originals = await (await self.client.get("/memory/api/messages?device_id=one&q=奶茶")).json()
        self.assertEqual(1, originals["data"]["total"])
        self.assertEqual(1, originals["data"]["items"][0]["excluded"])
        self.assertEqual(1, self.handler.store.list_memories("two")["total"])

    async def test_foreign_origin_and_rebinding_hostname_are_rejected(self):
        response = await self.client.get("/memory/api/devices", headers={"Origin": "https://untrusted.example"})
        self.assertEqual(403, response.status)
        response = await self.client.get("/memory/api/devices", headers={"Host": "untrusted.example"})
        self.assertEqual(403, response.status)
        response = await self.client.get("/memory/api/devices", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(403, response.status)

    async def test_configured_management_token_is_required_even_on_localhost(self):
        self.handler.options["management_token"] = "test-owner-token"
        self.assertEqual(401, (await self.client.get("/memory/api/devices")).status)
        self.assertEqual(401, (await self.client.get("/memory/api/devices", headers={"Authorization": "Bearer wrong"})).status)
        self.assertEqual(200, (await self.client.get("/memory/api/devices", headers={"Authorization": "Bearer test-owner-token"})).status)

    async def test_bad_parameters_and_invalid_json_do_not_change_data(self):
        self.assertEqual(400, (await self.client.get("/memory/api/memories")).status)
        self.assertEqual(400, (await self.client.get("/memory/api/messages?device_id=one&offset=bad")).status)
        path = f"/memory/api/memories/{self.memory['id']}?device_id=one"
        for data in [{"content": ""}, {"category": "invalid"}, {"follow_up_at": "bad"}, {"status": "deleted"}]:
            response = await self.client.patch(path, json=data)
            self.assertEqual(400, response.status)
        response = await self.client.patch(path, data="not json", headers={"Content-Type": "application/json"})
        self.assertEqual(400, response.status)
        self.assertEqual(1, self.handler.store.list_memories("one")["total"])

    async def test_story_crud_is_authenticated_scoped_and_preserves_user_memory(self):
        self.handler.options["character"] = {"enabled": True}
        path = "/memory/api/stories?device_id=one"
        response = await self.client.post(path, json={"title": "一只纸水母", "content": "我在菠萝屋陪小蜗折了一只纸水母。"})
        self.assertEqual(200, response.status)
        identity = (await response.json())["data"]["id"]
        detail_path = f"/memory/api/stories/{identity}?device_id=one"
        listed = (await (await self.client.get(path + "&q=纸水母")).json())["data"]
        self.assertTrue(any(row["id"] == identity for row in listed["items"]))
        foreign = await self.client.get(f"/memory/api/stories/{identity}?device_id=two")
        self.assertEqual(404, foreign.status)
        response = await self.client.patch(detail_path, json={"title": "小蜗的纸水母"})
        self.assertEqual(200, response.status)
        self.assertEqual("一只纸水母", (await response.json())["data"]["revisions"][0]["title"])
        self.assertEqual(200, (await self.client.delete(detail_path)).status)
        self.assertEqual(404, (await self.client.get(detail_path)).status)
        self.assertEqual(1, self.handler.store.list_memories("one")["total"])
        self.assertEqual(1, self.handler.store.list_messages("one")["total"])
        for data in [{"content": ""}, {"content": "故事", "origin": "generated"}, []]:
            self.assertEqual(400, (await self.client.post(path, json=data)).status)
        self.handler.options["management_token"] = "test-owner-token"
        self.assertEqual(401, (await self.client.post(path, json={"content": "故事"})).status)

    async def test_story_routes_are_unavailable_when_character_disabled(self):
        self.assertEqual(404, (await self.client.get("/memory/api/stories?device_id=one")).status)


if __name__ == "__main__":
    unittest.main()

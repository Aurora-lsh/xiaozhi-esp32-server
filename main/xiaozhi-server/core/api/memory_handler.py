"""Same-origin management UI for the local memory database."""

import asyncio
import hmac
import ipaddress
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from core.memory.store import LocalMemoryStore, database_path


class MemoryHandler:
    def __init__(self, config):
        self.options = config.get("local_memory", {})
        self.store = LocalMemoryStore(database_path(self.options))
        self.assets = Path(__file__).resolve().parent / "memory_web"

    def register(self, app):
        app.router.add_get("/memory", self.redirect)
        app.router.add_get("/memory/", self.page)
        app.router.add_get("/memory/app.js", self.script)
        app.router.add_route("*", "/memory/api/{resource:devices|memories|messages|stories}", self.api)
        app.router.add_route("*", "/memory/api/{resource:memories|stories}/{memory_id}", self.api)

    async def redirect(self, request):
        raise web.HTTPFound("/memory/")

    async def page(self, request):
        return web.FileResponse(self.assets / "index.html", headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })

    async def script(self, request):
        return web.FileResponse(self.assets / "app.js", headers={"Cache-Control": "no-store"})

    def authorize(self, request):
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden(text="仅允许同源访问")
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden(text="仅允许同源访问")
        token = self.options.get("management_token", "")
        if token:
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + token).encode("utf-8")):
                raise web.HTTPUnauthorized(text="请输入正确的记忆管理口令")
        else:
            # Do not trust forwarded headers: an unauthenticated owner interface
            # is only available over an actual loopback connection and hostname.
            try:
                peer = ipaddress.ip_address(request.remote or "")
                host = urlsplit("//" + request.host).hostname
                loopback_host = host == "localhost" or ipaddress.ip_address(host).is_loopback
                if not peer.is_loopback or not loopback_host:
                    raise ValueError()
            except (ValueError, TypeError):
                raise web.HTTPForbidden(text="请在本机通过 localhost 打开；远程管理需要配置 management_token")

    async def api(self, request):
        try:
            self.authorize(request)
            resource = request.match_info.get("resource", "memories")
            memory_id = request.match_info.get("memory_id")
            if resource == "devices":
                if request.method != "GET":
                    raise web.HTTPMethodNotAllowed(request.method, ["GET"])
                result = await asyncio.to_thread(self.store.devices)
            else:
                device = request.query.get("device_id", "").strip()
                if not device or len(device) > 200:
                    raise ValueError("请选择设备")
                if resource == "stories":
                    result = await self.stories(request, device, memory_id)
                elif memory_id:
                    if request.method == "GET":
                        result = await asyncio.to_thread(self.store.detail, device, memory_id)
                    elif request.method == "PATCH":
                        changes = await request.json()
                        if not isinstance(changes, dict):
                            raise ValueError("请求必须为JSON对象")
                        result = await asyncio.to_thread(self.store.edit, device, memory_id, changes)
                    elif request.method == "DELETE":
                        await asyncio.to_thread(self.store.delete, device, memory_id)
                        result = {"deleted": True}
                    else:
                        raise web.HTTPMethodNotAllowed(request.method, ["GET", "PATCH", "DELETE"])
                else:
                    if request.method != "GET":
                        raise web.HTTPMethodNotAllowed(request.method, ["GET"])
                    query = request.query.get("q", "")[:500]
                    limit = min(100, max(1, int(request.query.get("limit", "30"))))
                    offset = max(0, int(request.query.get("offset", "0")))
                    if resource == "messages":
                        result = await asyncio.to_thread(self.store.list_messages, device, query,
                            request.query.get("session_id", ""), limit, offset)
                    else:
                        result = await asyncio.to_thread(self.store.list_memories, device, query, limit, offset)
            response = web.json_response({"success": True, "data": result})
        except KeyError:
            response = web.json_response({"success": False, "message": "记忆不存在"}, status=404)
        except (ValueError, TypeError, sqlite3.IntegrityError) as error:
            response = web.json_response({"success": False, "message": str(error)}, status=400)
        except web.HTTPException as error:
            response = web.json_response({"success": False, "message": error.text}, status=error.status)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def stories(self, request, device, identity):
        from core.character.stories import CharacterStoryBook
        options = self.options.get("character", {})
        if not options.get("enabled", False):
            raise web.HTTPNotFound(text="角色故事尚未开启")
        devices = await asyncio.to_thread(self.store.devices)
        if not any(item["device_id"] == device for item in devices):
            raise KeyError("设备不存在")
        book = await asyncio.to_thread(CharacterStoryBook, self.store, device, options)
        if identity:
            if request.method == "GET":
                return await asyncio.to_thread(book.detail, identity)
            if request.method == "PATCH":
                return await asyncio.to_thread(book.edit, identity, await request.json())
            if request.method == "DELETE":
                await asyncio.to_thread(book.delete, identity)
                return {"deleted": True}
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "PATCH", "DELETE"])
        if request.method == "POST":
            data = await request.json()
            if not isinstance(data, dict) or set(data) - {"title", "content"}:
                raise ValueError("只能提交故事标题与内容")
            identity = await asyncio.to_thread(book.create, data.get("content"), data.get("title", "比奇堡的新日常"))
            return await asyncio.to_thread(book.detail, identity)
        if request.method == "GET":
            return await asyncio.to_thread(book.list, request.query.get("q", "")[:500],
                min(100, max(1, int(request.query.get("limit", "30")))),
                max(0, int(request.query.get("offset", "0"))))
        raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])

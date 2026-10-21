"""Check discovery and execution through the MCP endpoint, without printing tokens."""

import argparse
import asyncio
import json
import os
from pathlib import Path

import websockets


def endpoint_url():
    url = os.environ.get("MCP_ENDPOINT")
    if not url:
        for line in (Path(__file__).parent / "private/calculator.env").read_text().splitlines():
            if line.startswith("MCP_ENDPOINT="):
                url = line.split("=", 1)[1]
                break
    if not url:
        raise RuntimeError("MCP_ENDPOINT is missing")
    return url.replace("/mcp/", "/call/")


async def verify(check_call=True):
    async with websockets.connect(endpoint_url(), open_timeout=8) as ws:
        async def request(request_id, method, params=None):
            message = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            await ws.send(json.dumps(message))
            async def receive_reply():
                while True:
                    reply = json.loads(await ws.recv())
                    if reply.get("id") == request_id:
                        if "error" in reply:
                            raise RuntimeError("MCP request failed")
                        return reply["result"]
            return await asyncio.wait_for(receive_reply(), timeout=10)

        await request(1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "xiaozhi-endpoint-verification", "version": "1.0"},
        })
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        listed = await request(2, "tools/list", {})
        names = [tool["name"] for tool in listed["tools"]]
        assert "endpoint_calculator" in names, "Calculator not discovered"
        if not check_call:
            return {"status": "ok", "tools": names}
        result = await request(3, "tools/call", {
            "name": "endpoint_calculator", "arguments": {"expression": "(128 + 256) * 3"},
        })
        assert not result.get("isError"), "Calculator returned an error"
        value = json.loads(next(part["text"] for part in result["content"] if part["type"] == "text"))
        assert value["result"] == 1152, "Incorrect calculation"
        return {"status": "ok", "tools": names, "expression": value["expression"], "result": value["result"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(verify(check_call=not args.health))
    except Exception as exc:
        # Connection exceptions can include URLs. Never print the capability token.
        print("MCP endpoint verification failed: " + type(exc).__name__)
        raise SystemExit(1)
    if not args.health:
        print(json.dumps(result, ensure_ascii=False))

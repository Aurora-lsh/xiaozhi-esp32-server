"""Run inside the XiaoZhi backend to check its real MCP endpoint client."""

import asyncio
import json
import os

from core.providers.tools.mcp_endpoint.mcp_endpoint_handler import (
    call_mcp_endpoint_tool,
    connect_mcp_endpoint,
)


async def verify():
    url = os.environ["MCP_ENDPOINT"].replace("/mcp/", "/call/")
    client = await connect_mcp_endpoint(url)
    assert client is not None, "Backend could not connect"
    try:
        async def wait_ready():
            while not await client.is_ready():
                await asyncio.sleep(0.1)
        await asyncio.wait_for(wait_ready(), timeout=12)
        assert client.has_tool("endpoint_calculator"), "Backend could not discover calculator"
        result = await call_mcp_endpoint_tool(
            client, "endpoint_calculator", {"expression": "(128 + 256) * 3"}, timeout=10,
        )
        # XiaoZhi's client returns the first text block, already extracted from JSON-RPC.
        value = json.loads(result)
        assert value["result"] == 1152, "Incorrect result"
        print(json.dumps({"backend_client": "ok", "tool": "endpoint_calculator", "result": 1152}))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(verify())

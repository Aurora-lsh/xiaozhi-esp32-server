"""Check real MCP discovery/calls, optionally using XiaoZhi's own client."""

import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def check_calls(call_tool):
    for expression, expected in [("(128 + 256) * 3", 1152), ("(17 * 23) + 89", 480), ("-2 ** 3 + 10 / 4", -5.5)]:
        result = await call_tool("calculator", {"expression": expression})
        assert not result.isError, result
        payload = json.loads(result.content[0].text)
        assert payload["result"] == expected, payload
        print(json.dumps(payload, ensure_ascii=False))

    for expression in ["1 / 0", "2 ** 101", "True + 1", "__import__('os').getcwd()", "(1).__class__", "1e101", "(-1) ** 0.5"]:
        result = await call_tool("calculator", {"expression": expression})
        assert result.isError, f"Expression was not rejected: {expression}"
    print("Invalid expressions and code execution attempts rejected.")

    for name in ("get_beijing_time", "local_mcp_status"):
        result = await call_tool(name, {})
        assert not result.isError, result
        print(result.content[0].text)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8004/mcp")
    parser.add_argument("--xiaozhi", action="store_true")
    return parser.parse_args()


async def main(args, manager_class=None):
    expected_tools = {"calculator", "get_beijing_time", "local_mcp_status"}
    if args.xiaozhi:
        manager = manager_class(None)
        assert "xiaozhi-local-mcp" in manager.load_config(), "MCP config not installed"
        try:
            await manager.initialize_servers()
            names = {item["function"]["name"] for item in manager.get_all_tools()}
            assert expected_tools <= names, names
            print("XiaoZhi registered tools: " + ", ".join(sorted(names)))
            await check_calls(manager.execute_tool)
        finally:
            await manager.cleanup_all()
    else:
        async with streamablehttp_client(args.url) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                names = {item.name for item in (await session.list_tools()).tools}
                assert expected_tools == names, names
                print("MCP tools: " + ", ".join(sorted(names)))
                await check_calls(session.call_tool)
    print("PASS")


if __name__ == "__main__":
    args = parse_args()
    manager_class = None
    if args.xiaozhi:
        # XiaoZhi initializes its logging/config with asyncio.run at import time.
        # Import before starting our event loop, as its normal entry point does.
        from core.providers.tools.server_mcp.mcp_manager import ServerMCPManager

        manager_class = ServerMCPManager
    asyncio.run(asyncio.wait_for(main(args, manager_class), timeout=60))

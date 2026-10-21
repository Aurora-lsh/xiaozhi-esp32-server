"""Exercise a local Qwen -> XiaoZhi MCP -> Qwen text round trip."""

import argparse
import asyncio
import json

from openai import AsyncOpenAI

# XiaoZhi loads its logger/config at import time, before an event loop is running.
from core.providers.tools.server_mcp.mcp_manager import ServerMCPManager


async def main(args):
    manager = ServerMCPManager(None)
    try:
        await manager.initialize_servers()
        tools = [
            item for item in manager.get_all_tools()
            if item["function"]["name"] == "calculator"
        ]
        assert tools, "Calculator tool is unavailable"
        messages = [
            {"role": "system", "content": "你是中文助手。用户要求使用工具时，请实际调用工具，得到结果后用一句中文回答。"},
            {"role": "user", "content": "请使用计算器工具，计算一百二十八加二百五十六，再乘以三。"},
        ]
        async with AsyncOpenAI(base_url=args.base_url, api_key="local", timeout=120) as client:
            first = await client.chat.completions.create(
                model=args.model, messages=messages, tools=tools,
                tool_choice="auto", temperature=0, max_tokens=2048,
            )
            message = first.choices[0].message
            assert message.tool_calls, "Qwen did not return a structured tool call"
            messages.append(message.model_dump(exclude_none=True))
            saw_expected_result = False
            for call in message.tool_calls:
                assert call.function.name == "calculator", call.function.name
                arguments = json.loads(call.function.arguments)
                print("Qwen tool call: " + json.dumps({"name": call.function.name, "arguments": arguments}, ensure_ascii=False))
                result = await manager.execute_tool(call.function.name, arguments)
                assert not result.isError, result
                content = "\n".join(item.text for item in result.content if item.type == "text")
                payload = json.loads(content)
                assert payload["result"] == 1152, payload
                saw_expected_result = True
                print("MCP result: " + content)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            assert saw_expected_result
            second = await client.chat.completions.create(
                model=args.model, messages=messages, temperature=0, max_tokens=2048,
            )
            answer = second.choices[0].message.content
            assert answer, "Qwen returned an empty final response"
            print("Qwen answer: " + answer)
            assert "1152" in answer.replace(",", "") or "一千一百五十二" in answer, answer
        print("PASS: local Qwen -> XiaoZhi MCP calculator -> Chinese answer")
    finally:
        await manager.cleanup_all()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://host.docker.internal:1234/v1")
    parser.add_argument("--model", default="qwen/qwen3.5-9b")
    asyncio.run(main(parser.parse_args()))

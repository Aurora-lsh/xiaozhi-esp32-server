"""Expose the existing arithmetic evaluator through the WebSocket MCP relay."""

import sys

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, "/local-mcp")
from server import calculator as calculate_expression

mcp = FastMCP("xiaozhi-endpoint-calculator")


@mcp.tool()
def endpoint_calculator(expression: str) -> dict:
    """接入点计算器：进行算术计算，用工具结果回答数学问题。

    expression 支持数字、括号和 + - * / // % **，例如 (128 + 256) * 3。
    当用户指定使用 MCP 接入点计算器时，请调用此工具。
    """
    return calculate_expression(expression)


if __name__ == "__main__":
    mcp.run(transport="stdio")

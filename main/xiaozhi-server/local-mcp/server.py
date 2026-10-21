"""Local, read-only MCP tools for the XiaoZhi laboratory setup."""

import ast
from datetime import datetime
import math
import operator
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse


SERVICE_NAME = "xiaozhi-local-mcp"
BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
mcp = FastMCP(
    SERVICE_NAME,
    host="0.0.0.0",
    port=8004,
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", f"{SERVICE_NAME}:*"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
    ),
)


def checked_number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("只支持有限的实数。")
    if abs(value) > 1e100:
        raise ValueError("计算结果超出范围，绝对值不能大于 1e100。")
    return value


def evaluate(node):
    if isinstance(node, ast.Constant):
        return checked_number(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = evaluate(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and type(node.op) in BINARY_OPERATORS:
        left, right = evaluate(node.left), evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("指数的绝对值不能大于 100。")
        return checked_number(BINARY_OPERATORS[type(node.op)](left, right))
    raise ValueError("仅支持数字、括号及 + - * / // % ** 运算。")


@mcp.tool()
def calculator(expression: str) -> dict:
    """执行算术计算。数学计算请调用此工具，用返回结果回答。

    expression 使用数字和 + - * / // % ** 及括号，例如 (128 + 256) * 3。
    支持小数，使用 Python 常规浮点精度；不支持变量、函数或代码。
    """
    expression = expression.strip()
    if not expression or len(expression) > 256:
        raise ValueError("表达式长度必须在 1 到 256 个字符之间。")
    try:
        tree = ast.parse(expression, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 64:
            raise ValueError("表达式过于复杂，请拆分计算。")
        result = evaluate(tree.body)
    except (SyntaxError, ZeroDivisionError, OverflowError, RecursionError) as exc:
        raise ValueError("表达式无效、除数为零或计算超出范围。") from exc
    return {"expression": expression, "result": result}


@mcp.tool()
def get_beijing_time() -> dict:
    """查询当前北京时间（Asia/Shanghai），包括日期、时间和星期。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    return {
        "datetime": now.isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "weekday": "星期" + "一二三四五六日"[now.weekday()],
    }


@mcp.tool()
def local_mcp_status() -> dict:
    """检查本地 MCP 工具服务是否正常运行，返回服务名和工具列表。"""
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "tools": ["calculator", "get_beijing_time", "local_mcp_status"],
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    return JSONResponse({"status": "ok", "service": SERVICE_NAME})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

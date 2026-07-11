"""CaiBao utils MCP server, built on the official MCP Python SDK (FastMCP).

Spawned by CaiBao's MCPManager over stdio via config/mcp_servers.json.
Standalone smoke test:

    .venv/bin/python mcp-servers/utils/server.py
    (then speak JSON-RPC on stdin, or just check it starts without error)

Tool names must satisfy MCPManager's rule: ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$
and the namespaced form mcp__utils__{tool} must stay within 64 chars.
"""
from __future__ import annotations

import ast
import operator
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

# log_level=WARNING keeps stderr quiet: CaiBao's stdio client pipes stderr
# without draining it, so a chatty server could fill the pipe buffer.
mcp = FastMCP("caibao-utils", log_level="WARNING")


@mcp.tool()
def get_current_time(tz: str = "Asia/Shanghai") -> str:
    """获取当前日期时间（ISO 8601 格式）。tz 为 IANA 时区名，如 Asia/Shanghai、UTC、America/New_York。"""
    try:
        zone = ZoneInfo(tz)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {tz}") from exc
    now = datetime.now(zone)
    return f"{now.isoformat()} ({tz}, {now.strftime('%A')})"


_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_arith(node: ast.expr) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left = _eval_arith(node.left)
        right = _eval_arith(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e9):
            raise ValueError("Exponentiation operands too large.")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_eval_arith(node.operand))
    raise ValueError(f"Unsupported syntax: {type(node).__name__}")


@mcp.tool()
def calculate(expression: str) -> str:
    """计算四则运算表达式（支持 + - * / // % ** 和括号），如 "(3+5)*2/4"。"""
    if len(expression) > 500:
        raise ValueError("Expression too long (max 500 chars).")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"Invalid expression: {exc}") from exc
    result = _eval_arith(tree.body)
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return f"{expression} = {result}"


@mcp.tool()
def generate_uuid(count: int = 1) -> str:
    """生成 1-20 个随机 UUID (v4)。"""
    if not 1 <= count <= 20:
        raise ValueError("count must be between 1 and 20.")
    return "\n".join(str(uuid.uuid4()) for _ in range(count))


if __name__ == "__main__":
    mcp.run(transport="stdio")

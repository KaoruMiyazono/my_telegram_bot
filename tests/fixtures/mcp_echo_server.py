from __future__ import annotations

from mcp.server.fastmcp import FastMCP

server = FastMCP("m4-echo-test")


@server.tool()
def echo(text: str) -> str:
    """Return the supplied text for MCP runtime integration tests."""

    return f"echo:{text}"


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""

    return a + b


if __name__ == "__main__":
    server.run(transport="stdio")

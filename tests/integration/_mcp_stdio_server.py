"""A minimal MCP server over stdio, for the live transport test.

Run as a subprocess by test_mcp_live.py. Kept as its own file rather than a
`python -c` string so the stdio transport is exercised the way a customer's
server would actually be launched.
"""

try:  # mcp 2.x renamed the server class
    from mcp.server.mcpserver import MCPServer as Server
except ModuleNotFoundError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server

server = Server(name="probe-stdio")


@server.tool()
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return f"sum={a + b}"


if __name__ == "__main__":
    server.run(transport="stdio")

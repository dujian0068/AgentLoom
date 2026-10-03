import sys

from mcp.server.fastmcp import FastMCP

server = FastMCP("Integration Tool", host="127.0.0.1", port=int(sys.argv[1]))


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


server.run(transport="streamable-http")

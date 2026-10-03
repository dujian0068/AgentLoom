"""SDK for tools that run locally as MCP servers or in platform-managed containers."""

from mcp.server.fastmcp import FastMCP


class ToolServer(FastMCP):
    def serve(self):
        self.run(transport="stdio")


__all__ = ["ToolServer"]

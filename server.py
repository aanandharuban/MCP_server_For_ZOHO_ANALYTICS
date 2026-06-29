"""
Entry point for the Zoho Analytics MCP server.

Run with:
    fastmcp dev server.py          ← dev mode (hot-reload + UI on :8080)
    fastmcp run server.py          ← production stdio mode
"""

from src.tools import mcp

if __name__ == "__main__":
    mcp.run()

"""mcp-gui-tester — a PyQt6 GUI tester for Streamable-HTTP MCP servers."""

from .app import MCPClient, MCPTesterWindow, main

__version__ = "0.4.1"

__all__ = ["MCPClient", "MCPTesterWindow", "main", "__version__"]

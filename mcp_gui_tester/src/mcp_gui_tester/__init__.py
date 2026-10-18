"""mcp-gui-tester — a PySide6 GUI tester for Streamable-HTTP MCP servers."""

from .app import MCPClient, MCPTesterWindow, main

__version__ = "1.0.0a1"

__all__ = ["MCPClient", "MCPTesterWindow", "__version__", "main"]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MoleditPy MCP Server Plugin

Exposes MoleditPy's molecule operations via the Model Context Protocol (MCP),
enabling AI assistants such as Claude to query and control the molecular editor
over a local HTTP connection.

Installation:
    Copy (or symlink) the ``mcp_server/`` folder to your MoleditPy plugin directory:
      - Windows: C:\\Users\\<You>\\.moleditpy\\plugins\\mcp_server\\
      - Linux/macOS: ~/.moleditpy/plugins/mcp_server/

Usage:
    After installation, open MoleditPy and choose
    Plugins > MCP Server > Status & Settings...
    to start the server and obtain the configuration snippet for Claude Desktop.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

PLUGIN_NAME = "MCP Server"
PLUGIN_VERSION = "1.4.0"
PLUGIN_AUTHOR = "HiroYokoyama"
PLUGIN_DESCRIPTION = (
    "Expose MoleditPy via Model Context Protocol (MCP) "
    "for AI assistant integration (Claude Desktop, etc.)."
)
PLUGIN_CATEGORY = "Integration"
PLUGIN_TAGS = ["MCP", "AI", "Integration", "API", "Claude"]
PLUGIN_SUPPORTED_MOLEDITPY_VERSION = ">=4.0.0, <5.0.0"

logger = logging.getLogger(__name__)

_plugin: Optional["MCPServerPlugin"] = None


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


class MCPServerPlugin:
    """Manages the MCPBridge and MCPHttpServer lifecycle."""

    def __init__(self, context: Any) -> None:
        self.context = context
        self._bridge: Any = None
        self._server: Any = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, port: Optional[int] = None) -> bool:
        """Start the MCP server. Returns True on success."""
        if self._server is not None and self._server.is_running:
            self.context.show_status_message("MCP Server is already running.", 3000)
            return False

        if port is None:
            port = self.context.get_setting("port", 7891)

        try:
            from .bridge import MCPBridge  # pylint: disable=import-outside-toplevel
            from .server import MCPHttpServer  # pylint: disable=import-outside-toplevel
            self._bridge = MCPBridge(self.context)
            self._server = MCPHttpServer(
                self._bridge,
                server_name=PLUGIN_NAME,
                server_version=PLUGIN_VERSION,
                port=port,
            )
            self._server.start()
            self.context.show_status_message(
                f"MCP Server started at {self._server.url}", 5000
            )
            return True
        except Exception as exc:  # pylint: disable=broad-except
            # Broad on purpose: start() must never let an unexpected error
            # (import failure, missing PluginContext attribute, socket error,
            # etc.) escape into the menu-action callback and crash the app —
            # it always reports failure via the status bar instead.
            self.context.show_status_message(
                f"MCP Server failed to start: {exc}", 6000
            )
            logger.exception("MCP Server start failed")
            self._bridge = None
            self._server = None
            return False

    def stop(self) -> None:
        """Stop the MCP server."""
        if self._server is not None and self._server.is_running:
            self._server.stop()
            self.context.show_status_message("MCP Server stopped.", 3000)
        self._bridge = None
        self._server = None

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def show_status(self) -> None:
        """Open the status & settings dialog (singleton)."""
        win = self.context.get_window("status_dialog")
        if win is not None and win.isVisible():
            win.raise_()
            win.activateWindow()
            return
        from .ui import MCPStatusDialog  # pylint: disable=import-outside-toplevel
        dlg = MCPStatusDialog(self)
        self.context.register_window("status_dialog", dlg)
        dlg.show()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_running

    @property
    def url(self) -> str:
        if self._server is not None:
            return self._server.url
        port = self.context.get_setting("port", 7891)
        return f"http://127.0.0.1:{port}/mcp"


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def initialize(context: Any) -> None:
    """Called by MoleditPy when the plugin is loaded."""
    global _plugin
    _plugin = MCPServerPlugin(context)

    context.add_plugin_menu(
        "MCP Server/Status && Settings...", _plugin.show_status
    )
    context.add_plugin_menu("MCP Server/Start Server", _plugin.start)
    context.add_plugin_menu("MCP Server/Stop Server", _plugin.stop)

    if context.get_setting("auto_start", False):
        _plugin.start()

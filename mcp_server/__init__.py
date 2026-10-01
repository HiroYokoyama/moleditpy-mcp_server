#!/usr/bin/env python3
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
PLUGIN_VERSION = "1.8.5"
PLUGIN_AUTHOR = "HiroYokoyama"
PLUGIN_DESCRIPTION = (
    "Expose MoleditPy via Model Context Protocol (MCP) "
    "for AI assistant integration (Claude Desktop, etc.)."
)
PLUGIN_CATEGORY = "Integration"
PLUGIN_TAGS = ["AI"]
PLUGIN_SUPPORTED_MOLEDITPY_VERSION = ">=4.0.0, <5.0.0"

logger = logging.getLogger(__name__)

_HOST = "127.0.0.1"

#: Key under which the live plugin object is kept in the host's registry.
_HANDLE_KEY = "server_plugin"

_plugin: MCPServerPlugin | None = None


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

    def start(self, port: int | None = None) -> bool:
        """Start the MCP server. Returns True on success."""
        if self._server is not None and self._server.is_running:
            self.context.show_status_message("MCP Server is already running.", 3000)
            return False

        if port is None:
            port = self.context.get_setting("port", 7891)

        try:
            from .bridge import MCPBridge  # pylint: disable=import-outside-toplevel
            from .server import (  # pylint: disable=import-outside-toplevel
                MCPHttpServer,
                is_port_serving,
            )

            # Another MoleditPy already serves this port: binding would fail
            # (or, off Windows, split the traffic), so leave it be.
            if is_port_serving(_HOST, port):
                self.context.show_status_message(
                    f"MCP Server is already running in another MoleditPy "
                    f"instance (port {port}); not starting a second one.",
                    5000,
                )
                return False

            self._bridge = MCPBridge(self.context)
            self._server = MCPHttpServer(
                self._bridge,
                server_name=PLUGIN_NAME,
                server_version=PLUGIN_VERSION,
                port=port,
                protocol_mode=self.context.get_setting("protocol_mode", "auto"),
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
            self.context.show_status_message(f"MCP Server failed to start: {exc}", 6000)
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
    def external_port(self) -> int:
        """The port another instance is serving, or 0.

        Probed on each read, never cached: the other instance can exit at any
        time, and a stale answer would keep this one from ever starting.
        """
        if self.is_running:
            return 0
        from .server import is_port_serving  # pylint: disable=import-outside-toplevel

        port = self.context.get_setting("port", 7891)
        return port if is_port_serving(_HOST, port) else 0

    @property
    def url(self) -> str:
        if self._server is not None:
            return self._server.url
        port = self.context.get_setting("port", 7891)
        return f"http://{_HOST}:{port}/mcp"


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def _retire_previous_load(context: Any) -> bool:
    """Stop the server a previous load of this plugin left behind.

    The host reloads a plugin by re-executing its module and never calls a
    teardown hook, so the old server thread keeps its port and the new load
    could not bind it. The previous plugin object is kept in the host's
    per-plugin registry, which outlives the re-execution. Returns whether a
    server was running, so the new load can bring it back.
    """
    previous = context.get_window(_HANDLE_KEY)
    dialog = context.get_window("status_dialog")
    was_running = False
    if previous is not None:
        try:
            was_running = bool(previous.is_running)
            previous.stop()
        except Exception:  # pylint: disable=broad-except
            # Must not stop the new load: worst case the port stays taken and
            # start() reports that.
            logger.exception("Could not stop the MCP server of the previous load")
    if dialog is not None:
        # It drives the retired plugin object; a fresh one is built on demand.
        try:
            dialog.close()
        except Exception:  # pylint: disable=broad-except
            logger.debug("Could not close the previous status dialog", exc_info=True)
    return was_running


def initialize(context: Any) -> None:
    """Called by MoleditPy when the plugin is loaded."""
    global _plugin
    was_running = _retire_previous_load(context)
    _plugin = MCPServerPlugin(context)
    context.register_window(_HANDLE_KEY, _plugin)

    context.add_plugin_menu("MCP Server/Status && Settings...", _plugin.show_status)
    context.add_plugin_menu("MCP Server/Start Server", _plugin.start)
    context.add_plugin_menu("MCP Server/Stop Server", _plugin.stop)

    if was_running or context.get_setting("auto_start", False):
        _plugin.start()

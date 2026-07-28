#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Status and settings dialog for the MCP Server plugin."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

if TYPE_CHECKING:
    from mcp_server import MCPServerPlugin

# Client configuration templates. "{PORT}" is substituted verbatim (plain
# str.replace, so JSON braces need no escaping). Each entry:
# display name -> (template, where-to-put-it note).
_CLIENT_TEMPLATES = {
    "Claude Desktop": (
        """{
  "mcpServers": {
    "moleditpy": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:{PORT}/mcp"
    }
  }
}""",
        "Add to <i>claude_desktop_config.json</i>, then restart Claude Desktop.",
    ),
    "Claude Code (CLI)": (
        """{
  "mcpServers": {
    "moleditpy": {
      "type": "http",
      "url": "http://127.0.0.1:{PORT}/mcp"
    }
  }
}""",
        "Add to your Claude Code MCP configuration, or per-project "
        "<i>.claude/settings.json</i>.",
    ),
    "Cursor": (
        """{
  "mcpServers": {
    "moleditpy": {
      "url": "http://127.0.0.1:{PORT}/mcp"
    }
  }
}""",
        "Add to <i>~/.cursor/mcp.json</i> (global) or <i>.cursor/mcp.json</i> "
        "(project).",
    ),
    "Windsurf": (
        """{
  "mcpServers": {
    "moleditpy": {
      "serverUrl": "http://127.0.0.1:{PORT}/mcp"
    }
  }
}""",
        "Add to <i>~/.codeium/windsurf/mcp_config.json</i>.",
    ),
    "Zed": (
        """{
  "context_servers": {
    "moleditpy": {
      "url": "http://127.0.0.1:{PORT}/mcp"
    }
  }
}""",
        "Add to <i>~/.config/zed/settings.json</i>.",
    ),
    "VS Code (Copilot)": (
        """{
  "servers": {
    "moleditpy": {
      "type": "http",
      "url": "http://127.0.0.1:{PORT}/mcp"
    }
  }
}""",
        "Add to <i>.vscode/mcp.json</i> in your workspace (VS Code 1.101+).",
    ),
    "OpenAI Codex CLI": (
        """[mcp_servers.moleditpy]
url = "http://127.0.0.1:{PORT}/mcp\"""",
        "Add to <i>~/.codex/config.toml</i> (global) or "
        "<i>.codex/config.toml</i> (project).",
    ),
    "Google Antigravity": (
        """{
  "mcpServers": {
    "moleditpy": {
      "serverUrl": "http://127.0.0.1:{PORT}/mcp"
    }
  }
}""",
        "Add to <i>~/.gemini/antigravity/mcp_config.json</i>.",
    ),
    "curl (raw HTTP)": (
        """curl -s -X POST http://127.0.0.1:{PORT}/mcp \\
  -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'""",
        "Run from any shell to list the available tools.",
    ),
}


# Protocol mode choices: label -> (setting value, tooltip).
_PROTOCOL_MODES = (
    (
        "Auto — legacy handshake + MCP 2026-07-28",
        "auto",
        "Serve both eras on the same port: clients that send an 'initialize' "
        "handshake get the classic session protocol, clients that send "
        "per-request metadata get the stateless 2026-07-28 protocol. "
        "Recommended.",
    ),
    (
        "Legacy only (2024-11-05 … 2025-11-25)",
        "legacy",
        "Only the handshake-based protocol. Modern requests are rejected with "
        "an UnsupportedProtocolVersion error listing the legacy versions.",
    ),
    (
        "MCP 2026-07-28 only (stateless)",
        "modern",
        "Only the stateless 2026-07-28 protocol: no session id, mirrored "
        "MCP-Protocol-Version / Mcp-Method / Mcp-Name headers are required "
        "and validated, and 'initialize' is refused.",
    ),
)


def render_client_config(client: str, port: int) -> str:
    """Return the configuration snippet for *client* with *port* filled in."""
    template = _CLIENT_TEMPLATES[client][0]
    return template.replace("{PORT}", str(port))


class MCPStatusDialog(QDialog):
    """Dialog for viewing server status and configuring the MCP server."""

    def __init__(self, plugin: MCPServerPlugin) -> None:
        super().__init__(plugin.context.get_main_window())
        self._plugin = plugin
        self.setWindowTitle("MCP Server — Status & Settings")
        self.setMinimumWidth(480)
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Status indicator
        bold = QFont()
        bold.setBold(True)
        self._status_lbl = QLabel()
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setFont(bold)
        layout.addWidget(self._status_lbl)

        # Server URL (selectable)
        self._url_lbl = QLabel()
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._url_lbl)

        # Port row
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port:"))
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1024, 65535)
        self._port_spin.setValue(self._plugin.context.get_setting("port", 7891))
        self._port_spin.setToolTip(
            "The local port the MCP server listens on. "
            "Restart the server after changing."
        )
        self._port_spin.valueChanged.connect(lambda _v: self._update_config_view())
        port_row.addWidget(self._port_spin)
        port_row.addStretch()
        layout.addLayout(port_row)

        # Protocol version row
        proto_row = QHBoxLayout()
        proto_row.addWidget(QLabel("MCP protocol:"))
        self._protocol_combo = QComboBox()
        saved_mode = self._plugin.context.get_setting("protocol_mode", "auto")
        for label, value, tip in _PROTOCOL_MODES:
            self._protocol_combo.addItem(label, value)
            self._protocol_combo.setItemData(
                self._protocol_combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole
            )
        index = self._protocol_combo.findData(saved_mode)
        self._protocol_combo.setCurrentIndex(index if index >= 0 else 0)
        self._protocol_combo.setToolTip(
            "Which MCP protocol era the server speaks. "
            "Restart the server after changing."
        )
        self._protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        proto_row.addWidget(self._protocol_combo, 1)
        layout.addLayout(proto_row)

        # Auto-start checkbox
        self._auto_start_chk = QCheckBox("Auto-start server on launch")
        self._auto_start_chk.setChecked(
            self._plugin.context.get_setting("auto_start", False)
        )
        self._auto_start_chk.toggled.connect(self._on_auto_start_toggled)
        layout.addWidget(self._auto_start_chk)

        # File I/O base directory row
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("File I/O base dir:"))
        self._base_dir_edit = QLineEdit()
        self._base_dir_edit.setPlaceholderText("(unrestricted)")
        saved_dir = self._plugin.context.get_setting("file_io_base_dir", None)
        if saved_dir:
            self._base_dir_edit.setText(saved_dir)
        self._base_dir_edit.editingFinished.connect(self._on_base_dir_changed)
        dir_row.addWidget(self._base_dir_edit)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_base_dir)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

        # Copy URL button
        copy_btn = QPushButton("Copy Server URL")
        copy_btn.clicked.connect(self._copy_url)
        layout.addWidget(copy_btn)

        # Client configuration snippets (selector above the snippet view)
        client_row = QHBoxLayout()
        client_row.addWidget(QLabel("<b>Client configuration:</b>"))
        self._client_combo = QComboBox()
        self._client_combo.addItems(list(_CLIENT_TEMPLATES.keys()))
        self._client_combo.currentTextChanged.connect(self._on_client_changed)
        client_row.addWidget(self._client_combo, 1)
        copy_cfg_btn = QPushButton("Copy")
        copy_cfg_btn.setToolTip("Copy the snippet below to the clipboard")
        copy_cfg_btn.clicked.connect(self._copy_config)
        client_row.addWidget(copy_cfg_btn)
        layout.addLayout(client_row)

        self._config_view = QTextEdit()
        self._config_view.setReadOnly(True)
        self._config_view.setMaximumHeight(120)
        self._config_view.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 10px;"
        )
        layout.addWidget(self._config_view)

        self._config_note = QLabel()
        self._config_note.setWordWrap(True)
        self._config_note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(self._config_note)

        # Start / Stop button
        self._toggle_btn = QPushButton()
        self._toggle_btn.clicked.connect(self._toggle)
        layout.addWidget(self._toggle_btn)

        # Dialog buttons
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.close)
        layout.addWidget(btn_box)

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Update all controls to reflect the current server state."""
        running = self._plugin.is_running

        if running:
            self._status_lbl.setText("● Server Running")
            self._status_lbl.setStyleSheet("color: #00cc44; font-size: 13px;")
            self._toggle_btn.setText("Stop Server")
            self._port_spin.setEnabled(False)
            self._protocol_combo.setEnabled(False)
        else:
            self._status_lbl.setText("○ Server Stopped")
            self._status_lbl.setStyleSheet("color: #cc4444; font-size: 13px;")
            self._toggle_btn.setText("Start Server")
            self._port_spin.setEnabled(True)
            self._protocol_combo.setEnabled(True)

        url = self._plugin.url
        self._url_lbl.setText(url)
        self._update_config_view()

    def _update_config_view(self) -> None:
        client = self._client_combo.currentText()
        if client not in _CLIENT_TEMPLATES:
            return
        self._config_view.setPlainText(
            render_client_config(client, self._port_spin.value())
        )
        self._config_note.setText(_CLIENT_TEMPLATES[client][1])

    def _on_client_changed(self, _text: str) -> None:
        self._update_config_view()

    def _copy_config(self) -> None:
        QApplication.clipboard().setText(self._config_view.toPlainText())
        self._plugin.context.show_status_message(
            "Client configuration copied to clipboard.", 2000
        )

    def _toggle(self) -> None:
        if self._plugin.is_running:
            self._plugin.stop()
        else:
            port = self._port_spin.value()
            self._plugin.context.set_setting("port", port)
            self._plugin.start(port=port)
        self.refresh()

    def _on_protocol_changed(self, _index: int) -> None:
        mode = self._protocol_combo.currentData()
        self._plugin.context.set_setting("protocol_mode", mode)
        if self._plugin.is_running:
            self._plugin.context.show_status_message(
                "MCP protocol changed — restart the server to apply it.", 5000
            )

    def _on_auto_start_toggled(self, checked: bool) -> None:
        self._plugin.context.set_setting("auto_start", checked)

    def _on_base_dir_changed(self) -> None:
        text = self._base_dir_edit.text().strip()
        if not text:
            self._plugin.context.set_setting("file_io_base_dir", None)
            return
        path = Path(text).expanduser()
        if not path.is_dir():
            # Keep behavior consistent with the set_file_io_config MCP tool,
            # which rejects a base_dir that doesn't exist. Without this check
            # a typo here would silently sandbox the file I/O tools to a
            # directory that never resolves, so every write/read/list call
            # would fail with a confusing error instead of failing here.
            self._plugin.context.show_status_message(
                f"'{text}' is not an existing directory — "
                "File I/O base directory was not changed.",
                5000,
            )
            saved = self._plugin.context.get_setting("file_io_base_dir", None)
            self._base_dir_edit.setText(saved or "")
            return
        resolved = str(path.resolve())
        self._base_dir_edit.setText(resolved)
        self._plugin.context.set_setting("file_io_base_dir", resolved)

    def _browse_base_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select File I/O Base Directory", self._base_dir_edit.text() or ""
        )
        if directory:
            self._base_dir_edit.setText(directory)
            self._plugin.context.set_setting("file_io_base_dir", directory)

    def _copy_url(self) -> None:
        QApplication.clipboard().setText(self._plugin.url)
        self._plugin.context.show_status_message(
            "MCP server URL copied to clipboard.", 2000
        )

#!/usr/bin/env python3
"""Status and settings dialog for the MCP Server plugin."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
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


def render_client_config(client: str, port: int, token: str = "") -> str:
    """Return the configuration snippet for *client* with *port* filled in."""
    template = _CLIENT_TEMPLATES[client][0]
    rendered = template.replace("{PORT}", str(port))
    if not token:
        return rendered
    if client == "curl (raw HTTP)":
        return rendered.replace(
            '-H "Content-Type:',
            f'-H "Authorization: Bearer {token}" -H "Content-Type:',
        )
    if client == "OpenAI Codex CLI":
        return rendered + '\nhttp_headers = { Authorization = "Bearer ' + token + '" }'
    import json

    config = json.loads(rendered)
    section = next(iter(config.values()))
    section["moleditpy"]["headers"] = {"Authorization": "Bearer " + token}
    return json.dumps(config, indent=2)


class MCPStatusDialog(QDialog):
    """Dialog for viewing server status and configuring the MCP server."""

    def __init__(self, plugin: MCPServerPlugin) -> None:
        super().__init__(plugin.context.get_main_window())
        self._plugin = plugin
        self.setWindowTitle("MCP Server — Status & Settings")
        self.resize(760, 620)
        self.setMinimumWidth(520)
        self._build_ui()
        self.refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll_status)
        self._timer.start()

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

        self._feedback_lbl = QLabel()
        self._feedback_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._feedback_lbl.setWordWrap(True)
        layout.addWidget(self._feedback_lbl)
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, 1)
        server_page = QWidget()
        server_layout = QVBoxLayout(server_page)
        self._tabs.addTab(server_page, "Server")
        files_page = QWidget()
        files_layout = QVBoxLayout(files_page)
        self._tabs.addTab(files_page, "File access")
        client_page = QWidget()
        client_layout = QVBoxLayout(client_page)
        self._tabs.addTab(client_page, "Client configuration")

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
        self._port_spin.valueChanged.connect(lambda _v: self._on_port_changed())
        port_row.addWidget(self._port_spin)
        port_row.addStretch()
        server_layout.addLayout(port_row)

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
        self._protocol_combo.setCurrentIndex(max(index, 0))
        self._protocol_combo.setToolTip(
            "Which MCP protocol era the server speaks. "
            "Restart the server after changing."
        )
        self._protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        proto_row.addWidget(self._protocol_combo, 1)
        server_layout.addLayout(proto_row)

        # Auto-start checkbox
        self._auto_start_chk = QCheckBox("Auto-start server on launch")
        self._auto_start_chk.setChecked(
            self._plugin.context.get_setting("auto_start", False)
        )
        self._auto_start_chk.toggled.connect(self._on_auto_start_toggled)
        server_layout.addWidget(self._auto_start_chk)
        server_layout.addStretch()

        # File I/O base directory row
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("Read/write folder:"))
        self._base_dir_edit = QLineEdit()
        self._base_dir_edit.setPlaceholderText("(not set: file tools are disabled)")
        saved_dir = self._plugin.context.get_setting("file_io_base_dir", None)
        if saved_dir:
            self._base_dir_edit.setText(saved_dir)
        self._base_dir_edit.editingFinished.connect(self._on_base_dir_changed)
        dir_row.addWidget(self._base_dir_edit)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_base_dir)
        dir_row.addWidget(browse_btn)
        files_layout.addLayout(dir_row)
        note = QLabel(
            "The base folder allows reading and writing. Additional folders below allow reading only. Paths inside the read/write folder remain writable. Changes apply immediately."
        )
        note.setWordWrap(True)
        files_layout.addWidget(note)

        # Read-only folders: reading tools may use absolute paths inside them.
        # An MCP client can only *request* one (request_read_folder), which
        # the user approves in a dialog; adding or clearing here needs none.
        files_layout.addWidget(QLabel("Read-only folders"))
        self._read_roots_table = QTableWidget(0, 2)
        self._read_roots_table.setHorizontalHeaderLabels(["Folder path", "Status"])
        self._read_roots_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self._read_roots_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._read_roots_table.verticalHeader().hide()
        self._read_roots_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._read_roots_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._read_roots_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._read_roots_table.setAlternatingRowColors(True)
        self._read_roots_table.setWordWrap(False)
        self._read_roots_table.setAccessibleName("Read-only folders")
        self._read_roots_table.itemSelectionChanged.connect(
            self._on_read_root_selection
        )
        files_layout.addWidget(self._read_roots_table, 1)
        ro_row = QHBoxLayout()
        self._new_read_root_edit = QLineEdit()
        self._new_read_root_edit.setPlaceholderText(
            "Type a folder path, or leave empty and click Add to browse"
        )
        self._new_read_root_edit.setClearButtonEnabled(True)
        self._new_read_root_edit.setAccessibleName("Read-only folder to add")
        self._new_read_root_edit.returnPressed.connect(self._add_typed_read_root)
        ro_row.addWidget(self._new_read_root_edit, 1)
        add_ro_btn = QPushButton("Add…")
        add_ro_btn.setToolTip(
            "Add the typed folder; if the path box is empty, choose a folder."
        )
        add_ro_btn.clicked.connect(self._add_folder)
        ro_row.addWidget(add_ro_btn)
        files_layout.addLayout(ro_row)
        ro_actions = QHBoxLayout()
        self._remove_ro_btn = QPushButton("Remove selected")
        self._remove_ro_btn.clicked.connect(self._remove_read_roots)
        ro_actions.addWidget(self._remove_ro_btn)
        self._copy_ro_btn = QPushButton("Copy selected paths")
        self._copy_ro_btn.clicked.connect(self._copy_read_roots)
        ro_actions.addWidget(self._copy_ro_btn)
        ro_actions.addStretch()
        self._clear_ro_btn = QPushButton("Clear all…")
        self._clear_ro_btn.clicked.connect(self._clear_read_roots)
        ro_actions.addWidget(self._clear_ro_btn)
        files_layout.addLayout(ro_actions)
        self._shown_roots = None
        self._show_read_roots()

        # Copy URL button
        copy_btn = QPushButton("Copy Server URL")
        copy_btn.clicked.connect(self._copy_url)
        server_layout.addWidget(copy_btn)

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
        client_layout.addLayout(client_row)

        self._config_view = QTextEdit()
        self._config_view.setReadOnly(True)
        self._config_view.setMinimumHeight(180)
        self._config_view.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        client_layout.addWidget(self._config_view, 1)

        self._config_note = QLabel()
        self._config_note.setWordWrap(True)
        self._config_note.setStyleSheet("color: gray; font-size: 11px;")
        client_layout.addWidget(self._config_note)
        private_note = QLabel(
            "This configuration contains your private bearer token. Share it only with trusted clients."
        )
        private_note.setWordWrap(True)
        client_layout.addWidget(private_note)

        # Start / Stop button
        self._toggle_btn = QPushButton()
        self._toggle_btn.clicked.connect(self._toggle)
        action_row = QHBoxLayout()
        action_row.addWidget(self._toggle_btn)
        refresh_btn = QPushButton("Refresh status")
        refresh_btn.clicked.connect(self.refresh)
        action_row.addWidget(refresh_btn)
        layout.addLayout(action_row)

        # Dialog buttons
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_box.rejected.connect(self.close)
        layout.addWidget(btn_box)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

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
        elif self._plugin.external_port:
            self._status_lbl.setText("◐ Running in another MoleditPy instance")
            self._status_lbl.setStyleSheet("color: #cc9900; font-size: 13px;")
            self._toggle_btn.setText("Start Server")
            self._port_spin.setEnabled(True)
            self._protocol_combo.setEnabled(True)
        else:
            self._status_lbl.setText("○ Server Stopped")
            self._status_lbl.setStyleSheet("color: #cc4444; font-size: 13px;")
            self._toggle_btn.setText("Start Server")
            self._port_spin.setEnabled(True)
            self._protocol_combo.setEnabled(True)

        self._url_lbl.setText(self._display_url())
        self._last_running = running
        self._show_read_roots()
        self._update_config_view()

    def _update_config_view(self) -> None:
        client = self._client_combo.currentText()
        if client not in _CLIENT_TEMPLATES:
            return
        self._config_view.setPlainText(
            render_client_config(
                client, self._connection_port(), self._plugin.auth_token
            )
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
            if not self._plugin.start(port=port):
                self._feedback_lbl.setText(
                    getattr(self._plugin, "last_error", "")
                    or "Could not start the server. Check the main window status message."
                )
            else:
                self._feedback_lbl.clear()
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
            self._feedback_lbl.setText("Base folder cleared. File writes are disabled.")
            return
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
            text = text[1:-1]
        try:
            path = Path(text).expanduser()
            if not path.is_dir():
                raise ValueError("not an existing directory")
            resolved = str(path.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            message = f"'{text}' is not an existing directory — File I/O base directory was not changed. ({exc})"
            self._feedback_lbl.setText(message)
            self._plugin.context.show_status_message(message, 5000)
            saved = self._plugin.context.get_setting("file_io_base_dir", None)
            self._base_dir_edit.setText(saved or "")
            return
        self._base_dir_edit.setText(resolved)
        self._base_dir_edit.setCursorPosition(0)
        self._plugin.context.set_setting("file_io_base_dir", resolved)
        self._feedback_lbl.setText(
            "Base folder saved. Reading and writing are allowed inside this folder."
        )

    def _browse_base_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select File I/O Base Directory", self._base_dir_edit.text() or ""
        )
        if directory:
            # Same normalization as typing a path in the field.
            self._base_dir_edit.setText(directory)
            self._on_base_dir_changed()

    def _read_roots(self) -> list[str]:
        raw = self._plugin.context.get_setting("file_io_read_roots", None)
        return [r for r in raw if isinstance(r, str)] if isinstance(raw, list) else []

    def _show_read_roots(self) -> None:
        roots = self._read_roots()
        state = [(root, Path(root).is_dir()) for root in roots]
        if state == self._shown_roots:
            return
        selected = self._selected_read_roots()
        pending_path = self._new_read_root_edit.text()
        self._shown_roots = state
        self._read_roots_table.blockSignals(True)
        self._read_roots_table.setRowCount(len(roots))
        for row, (root, exists) in enumerate(state):
            path_item = QTableWidgetItem(root)
            path_item.setToolTip(root)
            self._read_roots_table.setItem(row, 0, path_item)
            self._read_roots_table.setItem(
                row, 1, QTableWidgetItem("Available" if exists else "Missing")
            )
        self._read_roots_table.clearSelection()
        for row, root in enumerate(roots):
            if root in selected:
                self._read_roots_table.item(row, 0).setSelected(True)
                self._read_roots_table.item(row, 1).setSelected(True)
        self._read_roots_table.blockSignals(False)
        self._clear_ro_btn.setEnabled(bool(roots))
        self._on_read_root_selection()
        if pending_path and pending_path not in selected:
            self._new_read_root_edit.setText(pending_path)

    def _selected_read_roots(self) -> list[str]:
        return [
            self._read_roots_table.item(index.row(), 0).text()
            for index in self._read_roots_table.selectionModel().selectedRows()
        ]

    def _on_read_root_selection(self) -> None:
        selected = self._selected_read_roots()
        self._new_read_root_edit.setText(selected[0] if selected else "")
        self._new_read_root_edit.setCursorPosition(0)
        self._remove_ro_btn.setEnabled(bool(selected))
        self._copy_ro_btn.setEnabled(bool(selected))

    def _save_read_root(self, directory: str) -> bool:
        text = directory.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
            text = text[1:-1]
        try:
            path = Path(text).expanduser()
            if not text or not path.is_dir():
                self._feedback_lbl.setText(
                    "Enter an existing folder. Read-only access was not changed."
                )
                return False
            resolved = str(path.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            self._feedback_lbl.setText(f"Could not use this folder: {exc}")
            return False
        roots = self._read_roots()
        if any(os.path.normcase(root) == os.path.normcase(resolved) for root in roots):
            self._feedback_lbl.setText("This folder already has read-only access.")
        else:
            roots.append(resolved)
            self._plugin.context.set_setting("file_io_read_roots", roots)
            self._feedback_lbl.setText("Read-only folder added.")
        self._show_read_roots()
        return True

    def _add_folder(self) -> None:
        if self._new_read_root_edit.text().strip():
            self._add_typed_read_root()
        else:
            self._add_read_root()

    def _add_typed_read_root(self) -> None:
        if self._save_read_root(self._new_read_root_edit.text()):
            self._new_read_root_edit.clear()

    def _add_read_root(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Add Read-only Folder for MCP", self._new_read_root_edit.text()
        )
        if directory:
            self._save_read_root(directory)

    def _remove_read_roots(self) -> None:
        selected = set(self._selected_read_roots())
        if selected:
            self._plugin.context.set_setting(
                "file_io_read_roots",
                [r for r in self._read_roots() if r not in selected],
            )
            self._feedback_lbl.setText(
                f"Removed read-only access to {len(selected)} folder(s)."
            )
            self._show_read_roots()

    def _copy_read_roots(self) -> None:
        QApplication.clipboard().setText("\n".join(self._selected_read_roots()))

    def _clear_read_roots(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Clear read-only folders",
                "Remove read-only access to all listed folders?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._plugin.context.set_setting("file_io_read_roots", [])
        self._feedback_lbl.setText("All read-only folder permissions removed.")
        self._show_read_roots()

    def _poll_status(self) -> None:
        # Local state and approvals refresh without a socket probe on the GUI thread.
        if self._plugin.is_running != self._last_running:
            self.refresh()
        else:
            self._show_read_roots()

    def closeEvent(self, event) -> None:
        self._timer.stop()
        super().closeEvent(event)

    def _connection_port(self) -> int:
        if self._plugin.is_running:
            from urllib.parse import urlsplit

            return urlsplit(self._plugin.url).port or self._port_spin.value()
        return self._port_spin.value()

    def _display_url(self) -> str:
        return f"http://127.0.0.1:{self._connection_port()}/mcp"

    def _on_port_changed(self) -> None:
        self._url_lbl.setText(self._display_url())
        self._update_config_view()

    def _copy_url(self) -> None:
        QApplication.clipboard().setText(self._display_url())
        self._plugin.context.show_status_message(
            "MCP server URL copied to clipboard.", 2000
        )

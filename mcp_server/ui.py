#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Status and settings dialog for the MCP Server plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
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

_CLAUDE_DESKTOP_CONFIG = """\
{
  "mcpServers": {
    "moleditpy": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:{port}/mcp"
    }
  }
}"""


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
        port_row.addWidget(self._port_spin)
        port_row.addStretch()
        layout.addLayout(port_row)

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

        # Claude Desktop configuration snippet
        layout.addWidget(QLabel("<b>Claude Desktop configuration:</b>"))
        self._config_view = QTextEdit()
        self._config_view.setReadOnly(True)
        self._config_view.setMaximumHeight(120)
        self._config_view.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 10px;"
        )
        layout.addWidget(self._config_view)

        note = QLabel(
            "Add the snippet above to <i>claude_desktop_config.json</i>, "
            "then restart Claude Desktop."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(note)

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
        port = self._port_spin.value()

        if running:
            self._status_lbl.setText("● Server Running")
            self._status_lbl.setStyleSheet("color: #00cc44; font-size: 13px;")
            self._toggle_btn.setText("Stop Server")
            self._port_spin.setEnabled(False)
        else:
            self._status_lbl.setText("○ Server Stopped")
            self._status_lbl.setStyleSheet("color: #cc4444; font-size: 13px;")
            self._toggle_btn.setText("Start Server")
            self._port_spin.setEnabled(True)

        url = self._plugin.url
        self._url_lbl.setText(url)
        self._config_view.setPlainText(
            _CLAUDE_DESKTOP_CONFIG.format(port=port)
        )

    def _toggle(self) -> None:
        if self._plugin.is_running:
            self._plugin.stop()
        else:
            port = self._port_spin.value()
            self._plugin.context.set_setting("port", port)
            self._plugin.start(port=port)
        self.refresh()

    def _on_auto_start_toggled(self, checked: bool) -> None:
        self._plugin.context.set_setting("auto_start", checked)

    def _on_base_dir_changed(self) -> None:
        text = self._base_dir_edit.text().strip()
        self._plugin.context.set_setting("file_io_base_dir", text or None)

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

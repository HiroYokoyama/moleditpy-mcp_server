#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP server tester — a standalone PyQt6 GUI for any MCP server that speaks
the Streamable HTTP transport (JSON-RPC over POST).

Connects to a running MCP server (default http://127.0.0.1:7891/mcp — the
MoleditPy MCP Server plugin), lists the available tools, builds a parameter
input form from each tool's inputSchema, and lets you call tools and inspect
the results. Host, port, and endpoint path are all editable, so it can be
pointed at any MCP HTTP server.

Usage:
    mcp-gui-tester [--url http://127.0.0.1:7891/mcp]
    python -m mcp_gui_tester [--url ...]

Requires PyQt6 only (uses urllib for HTTP).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

DEFAULT_URL = "http://127.0.0.1:7891/mcp"
REQUEST_TIMEOUT = 60.0  # generous: run_python may take up to 30 s server-side

# Property names that get a multi-line editor instead of a single line
_MULTILINE_HINTS = {"code", "content", "mol_block", "xyz_text"}


def _split_url(url: str) -> tuple:
    """Split an MCP endpoint URL into (host, port, path) for the input fields."""
    parsed = urllib.parse.urlparse(url if "//" in url else f"http://{url}")
    return (
        parsed.hostname or "127.0.0.1",
        parsed.port or 7891,
        parsed.path or "/mcp",
    )


def parse_json_param(name: str, expected_type: str, text: str) -> Any:
    """Parse *text* as JSON and validate it against ``array`` or ``object``."""
    text = text.strip()
    if not text:
        raise ValueError(f"Parameter {name!r}: JSON value is empty.")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Parameter {name!r}: invalid JSON ({exc})")
    expected = list if expected_type == "array" else dict
    if not isinstance(parsed, expected):
        raise ValueError(f"Parameter {name!r} must be a JSON {expected_type}.")
    return parsed


# ---------------------------------------------------------------------------
# JSON-RPC client (blocking; always called from a worker thread)
# ---------------------------------------------------------------------------


class MCPClient:
    """Minimal JSON-RPC client for the MCP Streamable HTTP transport."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._next_id = 0
        self._lock = threading.Lock()

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": msg_id}
        if params is not None:
            payload["params"] = params
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if "error" in data:
            err = data["error"]
            raise RuntimeError(f"JSON-RPC error {err.get('code')}: {err.get('message')}")
        return data.get("result")

    def initialize(self) -> Dict[str, Any]:
        return self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-tester", "version": "1.0"},
            },
        )

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._rpc("tools/list")
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self._rpc("tools/call", {"name": name, "arguments": arguments})


# ---------------------------------------------------------------------------
# Background worker: runs a callable in a thread, reports back via signals
# ---------------------------------------------------------------------------


class _Worker(QObject):
    """Runs a blocking callable off the GUI thread and emits the outcome."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def run_async(self, fn, *args: Any) -> None:
        def _target() -> None:
            try:
                self.finished.emit(fn(*args))
            except urllib.error.URLError as exc:
                self.failed.emit(f"Connection failed: {exc.reason}")
            except Exception as exc:  # pylint: disable=broad-except
                self.failed.emit(str(exc))

        threading.Thread(target=_target, daemon=True).start()


# ---------------------------------------------------------------------------
# Parameter form widgets
# ---------------------------------------------------------------------------


class ParamField:
    """One schema property bound to its input widget(s)."""

    def __init__(
        self,
        name: str,
        schema: Dict[str, Any],
        required: bool,
    ) -> None:
        self.name = name
        self.schema = schema
        self.required = required
        self.type = schema.get("type", "string")
        self.include_box: Optional[QCheckBox] = None
        self.widget = self._build_widget()

    def _build_widget(self) -> QWidget:
        default = self.schema.get("default")
        if "enum" in self.schema:
            combo = QComboBox()
            combo.addItems([str(v) for v in self.schema["enum"]])
            if default is not None:
                combo.setCurrentText(str(default))
            w: QWidget = combo
        elif self.type == "boolean":
            box = QCheckBox()
            if isinstance(default, bool):
                box.setChecked(default)
            w = box
        elif self.type == "integer":
            spin = QSpinBox()
            spin.setRange(-2_000_000_000, 2_000_000_000)
            if isinstance(default, int):
                spin.setValue(default)
            w = spin
        elif self.type == "number":
            dspin = QDoubleSpinBox()
            dspin.setRange(-1e12, 1e12)
            dspin.setDecimals(6)
            if isinstance(default, (int, float)):
                dspin.setValue(float(default))
            w = dspin
        elif self.type in ("array", "object"):
            edit = QPlainTextEdit()
            edit.setPlaceholderText(
                "[1, 2, 3]" if self.type == "array" else '{"0": "#FF0000"}'
            )
            edit.setFixedHeight(60)
            w = edit
        elif self.name in _MULTILINE_HINTS:
            edit = QPlainTextEdit()
            edit.setFixedHeight(120)
            if isinstance(default, str):
                edit.setPlainText(default)
            w = edit
        else:
            line = QLineEdit()
            if default is not None:
                line.setText(str(default))
            w = line
        w.setToolTip(self.schema.get("description", ""))
        return w

    def value(self) -> Any:
        """Return the field's current value, parsing JSON for array/object types."""
        if isinstance(self.widget, QComboBox):
            return self.widget.currentText()
        if isinstance(self.widget, QCheckBox):
            return self.widget.isChecked()
        if isinstance(self.widget, QSpinBox) or isinstance(self.widget, QDoubleSpinBox):
            return self.widget.value()
        if isinstance(self.widget, QPlainTextEdit):
            text = self.widget.toPlainText()
        else:
            text = self.widget.text()
        if self.type in ("array", "object"):
            return parse_json_param(self.name, self.type, text)
        return text

    def is_included(self) -> bool:
        """Whether this field should be sent (optional fields have a checkbox)."""
        return self.include_box is None or self.include_box.isChecked()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MCPTesterWindow(QMainWindow):
    """Tool browser + parameter form + call results."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self.setWindowTitle("MCP Server Tester")
        self.resize(1050, 700)
        self.client: Optional[MCPClient] = None
        self.tools: List[Dict[str, Any]] = []
        self.fields: List[ParamField] = []
        self.worker = _Worker()

        # --- top bar: host/port + connect ------------------------------
        host, port, path = _split_url(url)
        top = QWidget()
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(8, 8, 8, 4)
        top_lay.addWidget(QLabel("Host:"))
        self.host_edit = QLineEdit(host)
        top_lay.addWidget(self.host_edit, stretch=1)
        top_lay.addWidget(QLabel("Port:"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(port)
        top_lay.addWidget(self.port_spin)
        top_lay.addWidget(QLabel("Path:"))
        self.path_edit = QLineEdit(path)
        self.path_edit.setMaximumWidth(120)
        top_lay.addWidget(self.path_edit)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect)
        top_lay.addWidget(self.connect_btn)
        self.status_label = QLabel("Not connected")
        top_lay.addWidget(self.status_label)

        # --- left: tool list with filter ------------------------------
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter tools…")
        self.filter_edit.textChanged.connect(self._apply_filter)
        left_lay.addWidget(self.filter_edit)
        self.tool_list = QListWidget()
        self.tool_list.currentItemChanged.connect(self._on_tool_selected)
        left_lay.addWidget(self.tool_list)

        # --- right: description + params + call + result --------------
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)

        self.desc_label = QLabel("Connect to a server and select a tool.")
        self.desc_label.setWordWrap(True)
        self.desc_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        right_lay.addWidget(self.desc_label)

        self.params_group = QGroupBox("Parameters")
        self.params_form = QFormLayout(self.params_group)
        params_scroll = QScrollArea()
        params_scroll.setWidgetResizable(True)
        params_scroll.setWidget(self.params_group)
        right_lay.addWidget(params_scroll, stretch=1)

        self.call_btn = QPushButton("Call Tool")
        self.call_btn.setEnabled(False)
        self.call_btn.clicked.connect(self._on_call)
        right_lay.addWidget(self.call_btn)

        self.result_tabs = QTabWidget()
        mono = QFont("Consolas" if sys.platform == "win32" else "Monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.result_text = QPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setFont(mono)
        self.raw_text = QPlainTextEdit()
        self.raw_text.setReadOnly(True)
        self.raw_text.setFont(mono)
        self.result_tabs.addTab(self.result_text, "Result")
        self.result_tabs.addTab(self.raw_text, "Raw JSON")
        right_lay.addWidget(self.result_tabs, stretch=2)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(8, 0, 8, 8)
        lay.addWidget(top)
        lay.addWidget(splitter, stretch=1)
        self.setCentralWidget(central)

    # ------------------------------------------------------------------
    # Connect / tool list
    # ------------------------------------------------------------------

    def _on_connect(self) -> None:
        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "MCP Tester", "Enter the server host first.")
            return
        path = self.path_edit.text().strip()
        if path and not path.startswith("/"):
            path = "/" + path
        url = f"http://{host}:{self.port_spin.value()}{path}"
        self.client = MCPClient(url)
        self.connect_btn.setEnabled(False)
        self.status_label.setText("Connecting…")

        def _connect(client: MCPClient) -> Dict[str, Any]:
            info = client.initialize()
            tools = client.list_tools()
            return {"info": info, "tools": tools}

        self._run(_connect, self.client, on_done=self._on_connected)

    def _on_connected(self, result: Dict[str, Any]) -> None:
        self.connect_btn.setEnabled(True)
        server = result["info"].get("serverInfo", {})
        self.tools = result["tools"]
        self.status_label.setText(
            f"Connected: {server.get('name', '?')} v{server.get('version', '?')} "
            f"— {len(self.tools)} tools"
        )
        self._populate_tool_list()

    def _populate_tool_list(self) -> None:
        self.tool_list.clear()
        for tool in self.tools:
            item = QListWidgetItem(tool["name"])
            item.setToolTip(tool.get("description", ""))
            item.setData(Qt.ItemDataRole.UserRole, tool)
            self.tool_list.addItem(item)
        self._apply_filter(self.filter_edit.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.tool_list.count()):
            item = self.tool_list.item(i)
            tool = item.data(Qt.ItemDataRole.UserRole)
            haystack = (tool["name"] + " " + tool.get("description", "")).lower()
            item.setHidden(bool(needle) and needle not in haystack)

    # ------------------------------------------------------------------
    # Parameter form
    # ------------------------------------------------------------------

    def _on_tool_selected(
        self, current: Optional[QListWidgetItem], _prev: Optional[QListWidgetItem]
    ) -> None:
        self._clear_form()
        if current is None:
            self.call_btn.setEnabled(False)
            return
        tool = current.data(Qt.ItemDataRole.UserRole)
        self.desc_label.setText(
            f"<b>{tool['name']}</b><br>{tool.get('description', '')}"
        )
        schema = tool.get("inputSchema", {})
        props: Dict[str, Any] = schema.get("properties", {})
        required = set(schema.get("required", []))
        for prop_name, prop_schema in props.items():
            field = ParamField(prop_name, prop_schema, prop_name in required)
            label_text = prop_name + (" *" if field.required else "")
            if field.required:
                self.params_form.addRow(label_text, field.widget)
            else:
                row = QWidget()
                row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(0, 0, 0, 0)
                field.include_box = QCheckBox("send")
                field.include_box.setToolTip(
                    "Optional parameter — check to include it in the call."
                )
                field.widget.setEnabled(False)
                field.include_box.toggled.connect(field.widget.setEnabled)
                row_lay.addWidget(field.widget, stretch=1)
                row_lay.addWidget(field.include_box)
                self.params_form.addRow(label_text, row)
            hint = prop_schema.get("description", "")
            if hint:
                hint_label = QLabel(hint)
                hint_label.setWordWrap(True)
                hint_label.setStyleSheet("color: gray; font-size: 10px;")
                self.params_form.addRow("", hint_label)
            self.fields.append(field)
        if not props:
            self.params_form.addRow(QLabel("(no parameters)"))
        self.call_btn.setEnabled(True)

    def _clear_form(self) -> None:
        self.fields = []
        while self.params_form.rowCount():
            self.params_form.removeRow(0)

    # ------------------------------------------------------------------
    # Tool call
    # ------------------------------------------------------------------

    def _on_call(self) -> None:
        item = self.tool_list.currentItem()
        if item is None or self.client is None:
            return
        tool = item.data(Qt.ItemDataRole.UserRole)
        try:
            arguments = {
                f.name: f.value() for f in self.fields if f.is_included()
            }
        except ValueError as exc:
            QMessageBox.warning(self, "MCP Tester", str(exc))
            return
        self.call_btn.setEnabled(False)
        self.status_label.setText(f"Calling {tool['name']}…")
        self.result_text.setPlainText("")
        self.raw_text.setPlainText("")
        self._run(
            self.client.call_tool,
            tool["name"],
            arguments,
            on_done=self._on_call_done,
        )

    def _on_call_done(self, result: Dict[str, Any]) -> None:
        self.call_btn.setEnabled(True)
        self.status_label.setText("Done" if not result.get("isError") else "Tool error")
        texts = [
            block.get("text", "")
            for block in result.get("content", [])
            if block.get("type") == "text"
        ]
        prefix = "[TOOL ERROR]\n" if result.get("isError") else ""
        self.result_text.setPlainText(prefix + "\n".join(texts))
        self.raw_text.setPlainText(json.dumps(result, indent=2, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Worker plumbing
    # ------------------------------------------------------------------

    def _run(self, fn, *args: Any, on_done) -> None:
        """Run *fn* in a background thread; route outcome to the GUI thread."""
        try:
            self.worker.finished.disconnect()
        except TypeError:
            pass
        try:
            self.worker.failed.disconnect()
        except TypeError:
            pass
        self.worker.finished.connect(on_done)
        self.worker.failed.connect(self._on_error)
        self.worker.run_async(fn, *args)

    def _on_error(self, message: str) -> None:
        self.connect_btn.setEnabled(True)
        self.call_btn.setEnabled(self.tool_list.currentItem() is not None)
        self.status_label.setText("Error")
        QMessageBox.critical(self, "MCP Tester", message)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-gui-tester",
        description="GUI tester for MCP servers over Streamable HTTP",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"MCP endpoint (default {DEFAULT_URL})")
    args = parser.parse_args()
    app = QApplication(sys.argv)
    win = MCPTesterWindow(args.url)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

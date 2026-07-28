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
import base64
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPixmap
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

_HISTORY_MAX_TOOLS = 50

# --- MCP protocol ----------------------------------------------------------
#: Stateless revision: no handshake, per-request `_meta`, mirrored headers.
MODERN_PROTOCOL_VERSION = "2026-07-28"
#: Handshake revision requested by the legacy `initialize` path.
LEGACY_PROTOCOL_VERSION = "2024-11-05"

CLIENT_INFO = {"name": "mcp-gui-tester", "version": "0.5.0"}

#: Combo entries: label -> mode passed to MCPClient.
PROTOCOL_CHOICES = (
    ("Auto-detect", "auto"),
    (f"MCP {MODERN_PROTOCOL_VERSION} (stateless)", "modern"),
    ("Legacy handshake", "legacy"),
)

_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
_META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
_META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

ERR_METHOD_NOT_FOUND = -32601
ERR_HEADER_MISMATCH = -32020
ERR_UNSUPPORTED_PROTOCOL_VERSION = -32022
#: Error codes only a modern server emits — they identify the server's era.
MODERN_ERROR_CODES = frozenset({ERR_HEADER_MISMATCH, -32021, ERR_UNSUPPORTED_PROTOCOL_VERSION})


def encode_header_value(value: str) -> str:
    """Mirror *value* into a header, Base64-escaping it when it is not safe.

    Per the Streamable HTTP binding, values that are not plain printable
    ASCII (or that look like the sentinel itself) travel as
    ``=?base64?<b64>?=``.
    """
    needs_encoding = (
        value != value.strip()
        or any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in value)
        or (value.startswith("=?base64?") and value.endswith("?="))
    )
    if not needs_encoding:
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?base64?{encoded}?="


def format_annotations(tool: Dict[str, Any]) -> str:
    """Render a tool's behaviour hints as a compact, comma-separated label."""
    annotations = tool.get("annotations") or {}
    if not isinstance(annotations, dict):
        return ""
    parts: List[str] = []
    if annotations.get("readOnlyHint"):
        parts.append("read-only")
    else:
        if annotations.get("destructiveHint"):
            parts.append("destructive")
        if annotations.get("idempotentHint"):
            parts.append("idempotent")
    if annotations.get("openWorldHint"):
        parts.append("network")
    return ", ".join(parts)


def tool_color(tool: Dict[str, Any]) -> Optional[str]:
    """List colour for a tool: destructive stands out, read-only recedes."""
    annotations = tool.get("annotations") or {}
    if not isinstance(annotations, dict):
        return None
    if annotations.get("destructiveHint"):
        return "#cc4444"
    if annotations.get("readOnlyHint"):
        return "#4477cc"
    return None


def is_destructive(tool: Dict[str, Any]) -> bool:
    annotations = tool.get("annotations") or {}
    return bool(isinstance(annotations, dict) and annotations.get("destructiveHint"))


class MCPError(RuntimeError):
    """A JSON-RPC error returned by the server."""

    def __init__(
        self,
        code: int,
        message: str,
        data: Any = None,
        http_status: Optional[int] = None,
    ) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data
        self.http_status = http_status

    def details(self) -> str:
        """Multi-line rendering used in the error dialog."""
        text = f"JSON-RPC error {self.code}: {self.message}"
        if self.http_status is not None:
            text += f"\nHTTP status: {self.http_status}"
        if self.data is not None:
            text += "\n" + json.dumps(self.data, indent=2, ensure_ascii=False)
        return text


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


def _looks_like_json(text: str) -> bool:
    """Heuristic: does *text* look like it was meant to be parsed as JSON?"""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[0] in "[{\"":
        return True
    if stripped in ("true", "false", "null"):
        return True
    try:
        float(stripped)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Per-tool argument history (in-memory + persisted to a JSON file)
# ---------------------------------------------------------------------------


class ArgumentHistory:
    """Remembers the last-sent arguments for each tool, most-recent last.

    In-memory only by default (session-scoped); pass *path* to persist to a
    JSON file (capped at ``max_tools`` entries). A corrupt or unreadable
    history file is ignored silently rather than raised.
    """

    def __init__(self, path: Optional[Path] = None, max_tools: int = _HISTORY_MAX_TOOLS) -> None:
        # path=None (the default) keeps history in memory only for the
        # current session — nothing is written to disk. Pass a path to
        # opt in to persistence (used by the tests).
        self.path = path
        self.max_tools = max_tools
        self.data: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if isinstance(value, dict):
                        self.data[key] = value
        except Exception:  # pylint: disable=broad-except
            pass  # corrupt/unreadable history must never crash the app

    def get(self, tool_name: str) -> Optional[Dict[str, Any]]:
        return self.data.get(tool_name)

    def remember(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        self.data.pop(tool_name, None)
        self.data[tool_name] = arguments
        while len(self.data) > self.max_tools:
            self.data.popitem(last=False)
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
        except Exception:  # pylint: disable=broad-except
            pass


# ---------------------------------------------------------------------------
# JSON-RPC client (blocking; always called from a worker thread)
# ---------------------------------------------------------------------------


class MCPClient:
    """
    JSON-RPC client for the MCP Streamable HTTP transport, both eras.

    *protocol* selects how requests are framed:
      ``"modern"`` — 2026-07-28: no handshake, per-request ``_meta``, mirrored
      ``MCP-Protocol-Version`` / ``Mcp-Method`` / ``Mcp-Name`` headers.
      ``"legacy"`` — ``initialize`` handshake plus ``Mcp-Session-Id`` echo.
      ``"auto"``   — probe with ``server/discover`` and fall back to the
      handshake when the server does not know that method.
    """

    def __init__(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        protocol: str = "auto",
    ) -> None:
        self.url = url
        self.headers: Dict[str, str] = dict(headers or {})
        self.protocol = protocol if protocol in ("auto", "modern", "legacy") else "auto"
        self.era: Optional[str] = None
        self.protocol_version: Optional[str] = None
        self.supported_versions: List[str] = []
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self.instructions: str = ""
        self.session_id: Optional[str] = None
        self._next_id = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _post(
        self, payload: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """POST one JSON-RPC message; raise MCPError for JSON-RPC errors."""
        req_headers = {"Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream"}
        req_headers.update(extra_headers or {})
        req_headers.update(self.headers)
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=req_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read().decode("utf-8")
                session = resp.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                status = resp.status
        except urllib.error.HTTPError as exc:
            # A modern server reports version/header problems as 400 (and an
            # unknown method as 404) with a JSON-RPC error body — surface that
            # instead of a bare "HTTP Error 400".
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                raise RuntimeError(f"HTTP {exc.code}: {raw.strip() or exc.reason}") from exc
            self._raise_for_error(data, status)
            return data
        if not body.strip():
            return {}
        data = json.loads(body)
        self._raise_for_error(data, status)
        return data

    @staticmethod
    def _raise_for_error(data: Dict[str, Any], status: Optional[int]) -> None:
        if isinstance(data, dict) and "error" in data:
            err = data["error"] or {}
            raise MCPError(
                err.get("code", 0), err.get("message", ""), err.get("data"), status
            )

    def _next_message_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Send *method* using whichever era has been established."""
        if self.era == "modern" or (self.era is None and self.protocol == "modern"):
            return self._modern_rpc(method, params)
        return self._legacy_rpc(method, params)

    def _modern_rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        version = self.protocol_version or MODERN_PROTOCOL_VERSION
        body: Dict[str, Any] = dict(params or {})
        body["_meta"] = {
            _META_PROTOCOL_VERSION: version,
            _META_CLIENT_INFO: CLIENT_INFO,
            _META_CLIENT_CAPABILITIES: {},
        }
        extra = {"MCP-Protocol-Version": version, "Mcp-Method": method}
        if method == "tools/call" and "name" in body:
            extra["Mcp-Name"] = encode_header_value(str(body["name"]))
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._next_message_id(),
            "params": body,
        }
        try:
            return self._post(payload, extra).get("result")
        except MCPError as exc:
            if exc.code != ERR_UNSUPPORTED_PROTOCOL_VERSION:
                raise
            # The server told us what it speaks — adopt it and retry once.
            supported = (exc.data or {}).get("supported") or []
            self.supported_versions = list(supported)
            retry = next((v for v in supported if v >= MODERN_PROTOCOL_VERSION), None)
            if retry is None or retry == version:
                raise
            self.protocol_version = retry
            return self._modern_rpc(method, params)

    def _legacy_rpc(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._next_message_id(),
        }
        if params is not None:
            payload["params"] = params
        extra = {"Mcp-Session-Id": self.session_id} if self.session_id else {}
        return self._post(payload, extra).get("result")

    # ------------------------------------------------------------------
    # Connection / discovery
    # ------------------------------------------------------------------

    def connect(self) -> Dict[str, Any]:
        """
        Establish which era the server speaks and collect its metadata.

        Returns a summary dict: era, protocolVersion, serverInfo,
        capabilities, instructions, supportedVersions.
        """
        if self.protocol == "legacy":
            return self._connect_legacy()
        try:
            return self._connect_modern()
        except MCPError as exc:
            if self.protocol == "modern":
                raise
            if exc.code == ERR_UNSUPPORTED_PROTOCOL_VERSION:
                # Only fall back when the server advertises handshake-era
                # versions exclusively; a modern list means it really is a
                # modern server and the mismatch must surface.
                supported = (exc.data or {}).get("supported") or []
                if any(v >= MODERN_PROTOCOL_VERSION for v in supported):
                    raise
            elif exc.code != ERR_METHOD_NOT_FOUND:
                raise
        except RuntimeError:
            if self.protocol == "modern":
                raise
        return self._connect_legacy()

    def _connect_modern(self) -> Dict[str, Any]:
        self.era = "modern"
        self.protocol_version = self.protocol_version or MODERN_PROTOCOL_VERSION
        result = self._modern_rpc("server/discover") or {}
        self.supported_versions = result.get("supportedVersions", [])
        self.capabilities = result.get("capabilities", {})
        self.instructions = result.get("instructions", "")
        self.server_info = (result.get("_meta") or {}).get(_META_SERVER_INFO, {})
        if (
            self.supported_versions
            and self.protocol_version not in self.supported_versions
        ):
            self.protocol_version = self.supported_versions[0]
        return self.summary()

    def _connect_legacy(self) -> Dict[str, Any]:
        self.era = "legacy"
        result = self._legacy_rpc(
            "initialize",
            {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        ) or {}
        self.protocol_version = result.get("protocolVersion", LEGACY_PROTOCOL_VERSION)
        self.server_info = result.get("serverInfo", {})
        self.capabilities = result.get("capabilities", {})
        self.instructions = result.get("instructions", "")
        self.supported_versions = [self.protocol_version]
        return self.summary()

    def summary(self) -> Dict[str, Any]:
        return {
            "era": self.era,
            "protocolVersion": self.protocol_version,
            "serverInfo": self.server_info,
            "capabilities": self.capabilities,
            "instructions": self.instructions,
            "supportedVersions": self.supported_versions,
            "sessionId": self.session_id,
        }

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------

    def initialize(self) -> Dict[str, Any]:
        """Legacy handshake (kept for callers that want it explicitly)."""
        return self._connect_legacy()

    def ping(self) -> Any:
        return self._rpc("ping")

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._rpc("tools/list") or {}
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self._rpc("tools/call", {"name": name, "arguments": arguments})


# ---------------------------------------------------------------------------
# Background worker: runs a callable in a thread, reports back via signals
# ---------------------------------------------------------------------------


class _Worker(QObject):
    """Runs a blocking callable off the GUI thread and emits the outcome."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(object)

    def run_async(self, fn, *args: Any) -> None:
        def _target() -> None:
            try:
                self.finished.emit(fn(*args))
            except urllib.error.URLError as exc:
                self.failed.emit(f"Connection failed: {exc.reason}")
            except MCPError as exc:
                self.failed.emit(exc)
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
        self.has_explicit_type = "type" in schema
        # Union schemas ({"oneOf": [{"type": "string"}, {"type": "array"...}]})
        # have no top-level "type". Track the allowed types so value() can
        # coerce to whichever alternative the user's input matches.
        self.union_types = frozenset(
            alt.get("type") for alt in schema.get("oneOf", []) if isinstance(alt, dict)
        )
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
        elif self.union_types and not self.has_explicit_type:
            w = self._build_oneof_widget(default)
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

    def _build_oneof_widget(self, default: Any) -> QWidget:
        """Pick a widget for a ``oneOf`` union schema (no top-level "type")."""
        if "array" in self.union_types or "object" in self.union_types:
            edit = QPlainTextEdit()
            edit.setFixedHeight(120)
            if "array" in self.union_types:
                edit.setPlaceholderText(
                    'Multi-line text, or a JSON array of lines: ["line1", "line2"]'
                )
            else:
                edit.setPlaceholderText('Text, or a JSON object: {"key": "value"}')
            if isinstance(default, str):
                edit.setPlainText(default)
            return edit
        if "string" in self.union_types:
            if self.name in _MULTILINE_HINTS:
                edit = QPlainTextEdit()
                edit.setFixedHeight(120)
                if isinstance(default, str):
                    edit.setPlainText(default)
                return edit
            line = QLineEdit()
            if default is not None:
                line.setText(str(default))
            return line
        # No array/object/string alternative: fall back to the first
        # alternative's own widget type (boolean/integer/number/other).
        first_type = None
        for alt in self.schema.get("oneOf", []):
            if isinstance(alt, dict) and alt.get("type"):
                first_type = alt.get("type")
                break
        if first_type == "boolean":
            box = QCheckBox()
            if isinstance(default, bool):
                box.setChecked(default)
            return box
        if first_type == "integer":
            spin = QSpinBox()
            spin.setRange(-2_000_000_000, 2_000_000_000)
            if isinstance(default, int):
                spin.setValue(default)
            return spin
        if first_type == "number":
            dspin = QDoubleSpinBox()
            dspin.setRange(-1e12, 1e12)
            dspin.setDecimals(6)
            if isinstance(default, (int, float)):
                dspin.setValue(float(default))
            return dspin
        line = QLineEdit()
        if default is not None:
            line.setText(str(default))
        return line

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
        if self.union_types and not self.has_explicit_type:
            return self._coerce_union_value(text)
        return text

    def _coerce_union_value(self, text: str) -> Any:
        """For a oneOf field, parse JSON-looking text if it matches an
        allowed alternative type; otherwise pass the raw string through."""
        if not _looks_like_json(text):
            return text
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        py_type = {
            list: "array",
            dict: "object",
            bool: "boolean",
            int: "integer",
            float: "number",
            str: "string",
            type(None): "null",
        }.get(type(parsed))
        if py_type in self.union_types:
            return parsed
        if py_type == "integer" and "number" in self.union_types:
            return parsed
        return text

    def is_included(self) -> bool:
        """Whether this field should be sent (optional fields have a checkbox)."""
        return self.include_box is None or self.include_box.isChecked()

    def set_value(self, value: Any) -> None:
        """Populate the widget from a previously-sent argument value
        (used to prefill a form from per-tool argument history)."""
        if isinstance(self.widget, QComboBox):
            self.widget.setCurrentText(str(value))
        elif isinstance(self.widget, QCheckBox):
            self.widget.setChecked(bool(value))
        elif isinstance(self.widget, QSpinBox):
            try:
                self.widget.setValue(int(value))
            except (TypeError, ValueError):
                pass
        elif isinstance(self.widget, QDoubleSpinBox):
            try:
                self.widget.setValue(float(value))
            except (TypeError, ValueError):
                pass
        elif isinstance(self.widget, QPlainTextEdit):
            if isinstance(value, (list, dict)):
                self.widget.setPlainText(json.dumps(value, indent=2, ensure_ascii=False))
            else:
                self.widget.setPlainText(str(value))
        else:
            self.widget.setText(str(value))
        if self.include_box is not None:
            self.include_box.setChecked(True)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MCPTesterWindow(QMainWindow):
    """Tool browser + parameter form + call results."""

    def __init__(self, url: str, protocol: str = "auto") -> None:
        super().__init__()
        self.setWindowTitle("MCP Server Tester")
        self.resize(1050, 700)
        self.client: Optional[MCPClient] = None
        self.tools: List[Dict[str, Any]] = []
        self.fields: List[ParamField] = []
        self.worker = _Worker()
        self.history = ArgumentHistory()
        self._call_started: Optional[float] = None

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
        top_lay.addWidget(QLabel("Protocol:"))
        self.protocol_combo = QComboBox()
        for label, mode in PROTOCOL_CHOICES:
            self.protocol_combo.addItem(label, mode)
        self.protocol_combo.setToolTip(
            "How requests are framed. Auto-detect probes with server/discover "
            f"and falls back to the initialize handshake.\n"
            f"MCP {MODERN_PROTOCOL_VERSION} is stateless: per-request _meta and "
            "mirrored MCP-Protocol-Version / Mcp-Method / Mcp-Name headers."
        )
        proto_index = self.protocol_combo.findData(protocol)
        self.protocol_combo.setCurrentIndex(proto_index if proto_index >= 0 else 0)
        top_lay.addWidget(self.protocol_combo)
        top_lay.addWidget(QLabel("Headers:"))
        self.headers_edit = QLineEdit()
        self.headers_edit.setPlaceholderText('{"Authorization": "Bearer ..."}')
        self.headers_edit.setMaximumWidth(200)
        top_lay.addWidget(self.headers_edit)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self._on_connect)
        top_lay.addWidget(self.connect_btn)
        self.refresh_btn = QPushButton("Refresh tools")
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.clicked.connect(self._on_refresh_tools)
        top_lay.addWidget(self.refresh_btn)

        # Status lives in the bottom status bar so long messages (connect
        # errors, tool results) can never stretch the top bar and shove the
        # buttons around.
        self.status_label = QLabel("Not connected")
        self.statusBar().addWidget(self.status_label, 1)

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

        call_row = QWidget()
        call_row_lay = QHBoxLayout(call_row)
        call_row_lay.setContentsMargins(0, 0, 0, 0)
        self.call_btn = QPushButton("Call Tool")
        self.call_btn.setEnabled(False)
        self.call_btn.clicked.connect(self._on_call)
        call_row_lay.addWidget(self.call_btn, stretch=1)
        self.reset_form_btn = QPushButton("Reset form")
        self.reset_form_btn.setEnabled(False)
        self.reset_form_btn.clicked.connect(self._on_reset_form)
        call_row_lay.addWidget(self.reset_form_btn)
        self.confirm_destructive_chk = QCheckBox("Confirm destructive")
        self.confirm_destructive_chk.setChecked(True)
        self.confirm_destructive_chk.setToolTip(
            "Ask before calling a tool the server marks as destructive "
            "(annotations.destructiveHint)."
        )
        call_row_lay.addWidget(self.confirm_destructive_chk)

        params_pane = QWidget()
        params_pane_lay = QVBoxLayout(params_pane)
        params_pane_lay.setContentsMargins(0, 0, 0, 0)
        params_pane_lay.addWidget(params_scroll, stretch=1)
        params_pane_lay.addWidget(call_row)

        self.result_tabs = QTabWidget()
        mono = QFont("Consolas" if sys.platform == "win32" else "Monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.result_text = QPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setFont(mono)

        self.result_extra = QWidget()
        self.result_extra_lay = QVBoxLayout(self.result_extra)
        self.result_extra_lay.setContentsMargins(0, 0, 0, 0)
        self.result_extra_scroll = QScrollArea()
        self.result_extra_scroll.setWidgetResizable(True)
        self.result_extra_scroll.setWidget(self.result_extra)
        # Hidden until a call actually returns image/resource content, so
        # the empty pane doesn't waste half the Result tab.
        self.result_extra_scroll.setVisible(False)

        result_page = QWidget()
        result_page_lay = QVBoxLayout(result_page)
        result_page_lay.setContentsMargins(0, 0, 0, 0)
        result_page_lay.addWidget(self.result_text, stretch=1)
        result_page_lay.addWidget(self.result_extra_scroll, stretch=1)

        self.raw_text = QPlainTextEdit()
        self.raw_text.setReadOnly(True)
        self.raw_text.setFont(mono)
        self.server_text = QPlainTextEdit()
        self.server_text.setReadOnly(True)
        self.server_text.setFont(mono)
        self.server_text.setPlainText("Not connected.")
        self.result_tabs.addTab(result_page, "Result")
        self.result_tabs.addTab(self.raw_text, "Raw JSON")
        self.result_tabs.addTab(self.server_text, "Server")

        # Draggable vertical (height) split: parameter form on top takes
        # ~3/4 of the pane, result tabs on the bottom ~1/4.
        right_split = QSplitter(Qt.Orientation.Vertical)
        right_split.addWidget(params_pane)
        right_split.addWidget(self.result_tabs)
        right_split.setStretchFactor(0, 3)
        right_split.setStretchFactor(1, 1)
        # Stretch factors only govern resize distribution; set the initial
        # sizes explicitly so the form starts tall without a manual drag.
        right_split.setSizes([450, 150])
        right_lay.addWidget(right_split, stretch=1)

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
        headers_text = self.headers_edit.text().strip()
        headers: Dict[str, str] = {}
        if headers_text:
            try:
                parsed_headers = json.loads(headers_text)
            except json.JSONDecodeError as exc:
                QMessageBox.warning(self, "MCP Tester", f"Invalid headers JSON: {exc}")
                return
            if not isinstance(parsed_headers, dict):
                QMessageBox.warning(self, "MCP Tester", "Headers must be a JSON object.")
                return
            headers = parsed_headers
        path = self.path_edit.text().strip()
        if path and not path.startswith("/"):
            path = "/" + path
        url = f"http://{host}:{self.port_spin.value()}{path}"
        self.client = MCPClient(
            url, headers=headers, protocol=self.protocol_combo.currentData()
        )
        self.connect_btn.setEnabled(False)
        self.status_label.setText("Connecting…")

        def _connect(client: MCPClient) -> Dict[str, Any]:
            info = client.connect()
            tools = client.list_tools()
            return {"info": info, "tools": tools}

        self._run(_connect, self.client, on_done=self._on_connected)

    def _on_connected(self, result: Dict[str, Any]) -> None:
        self.connect_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        info = result["info"]
        server = info.get("serverInfo", {})
        self.tools = result["tools"]
        self.status_label.setText(
            f"Connected: {server.get('name', '?')} v{server.get('version', '?')} "
            f"— {info.get('era', '?')} / {info.get('protocolVersion', '?')} "
            f"— {len(self.tools)} tools"
        )
        self.server_text.setPlainText(self._format_server_info(info))
        self._populate_tool_list()

    @staticmethod
    def _format_server_info(info: Dict[str, Any]) -> str:
        """Human-readable summary for the Server tab."""
        lines = [
            f"Era:               {info.get('era', '?')}",
            f"Protocol version:  {info.get('protocolVersion', '?')}",
            f"Supported:         {', '.join(info.get('supportedVersions') or []) or '(not advertised)'}",
            f"Session id:        {info.get('sessionId') or '(stateless)'}",
            "",
            "serverInfo:",
            json.dumps(info.get("serverInfo") or {}, indent=2, ensure_ascii=False),
            "",
            "capabilities:",
            json.dumps(info.get("capabilities") or {}, indent=2, ensure_ascii=False),
        ]
        instructions = info.get("instructions")
        if instructions:
            lines += ["", "instructions:", instructions]
        return "\n".join(lines)

    def _on_refresh_tools(self) -> None:
        if self.client is None:
            return
        self.refresh_btn.setEnabled(False)
        self.status_label.setText("Refreshing tools…")
        self._run(self.client.list_tools, on_done=self._on_refreshed)

    def _on_refreshed(self, tools: List[Dict[str, Any]]) -> None:
        self.refresh_btn.setEnabled(True)
        current_item = self.tool_list.currentItem()
        current_name = (
            current_item.data(Qt.ItemDataRole.UserRole)["name"]
            if current_item is not None
            else None
        )
        self.tools = tools
        self.status_label.setText(f"Refreshed — {len(self.tools)} tools")
        self._populate_tool_list()
        if current_name is not None:
            for i in range(self.tool_list.count()):
                item = self.tool_list.item(i)
                if item.data(Qt.ItemDataRole.UserRole)["name"] == current_name:
                    self.tool_list.setCurrentRow(i)
                    break

    def _populate_tool_list(self) -> None:
        self.tool_list.clear()
        for tool in self.tools:
            item = QListWidgetItem(tool["name"])
            tip = tool.get("description", "")
            hints = format_annotations(tool)
            item.setToolTip(f"[{hints}]\n{tip}" if hints else tip)
            color = tool_color(tool)
            if color is not None:
                item.setForeground(QColor(color))
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
        if current is None:
            self._clear_form()
            self.call_btn.setEnabled(False)
            self.reset_form_btn.setEnabled(False)
            return
        tool = current.data(Qt.ItemDataRole.UserRole)
        self._build_form(tool, prefill=True)

    def _on_reset_form(self) -> None:
        item = self.tool_list.currentItem()
        if item is None:
            return
        self._build_form(item.data(Qt.ItemDataRole.UserRole), prefill=False)

    def _build_form(self, tool: Dict[str, Any], prefill: bool) -> None:
        self._clear_form()
        hints = format_annotations(tool)
        self.desc_label.setText(
            f"<b>{tool['name']}</b>"
            + (f" <i>[{hints}]</i>" if hints else "")
            + f"<br>{tool.get('description', '')}"
        )
        schema = tool.get("inputSchema", {})
        props: Dict[str, Any] = schema.get("properties", {})
        required = set(schema.get("required", []))
        remembered = self.history.get(tool["name"]) if prefill else None
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
            if remembered is not None and prop_name in remembered:
                field.set_value(remembered[prop_name])
            self.fields.append(field)
        if not props:
            self.params_form.addRow(QLabel("(no parameters)"))
        self.call_btn.setEnabled(True)
        self.reset_form_btn.setEnabled(True)

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
        if is_destructive(tool) and self.confirm_destructive_chk.isChecked():
            answer = QMessageBox.question(
                self,
                "MCP Tester",
                f"{tool['name']} is marked destructive by the server — it may "
                "overwrite or erase data.\n\nCall it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.status_label.setText("Call cancelled")
                return
        self.history.remember(tool["name"], arguments)
        self.call_btn.setEnabled(False)
        self._call_started = time.monotonic()
        self.status_label.setText(f"Calling {tool['name']}…")
        self.result_text.setPlainText("")
        self.raw_text.setPlainText("")
        self._clear_result_extras()
        self._run(
            self.client.call_tool,
            tool["name"],
            arguments,
            on_done=self._on_call_done,
        )

    def _on_call_done(self, result: Dict[str, Any]) -> None:
        self.call_btn.setEnabled(True)
        elapsed = ""
        if self._call_started is not None:
            elapsed = f" in {(time.monotonic() - self._call_started) * 1000:.0f} ms"
            self._call_started = None
        self.status_label.setText(
            ("Done" if not result.get("isError") else "Tool error") + elapsed
        )
        content = result.get("content", [])
        texts = [
            block.get("text", "")
            for block in content
            if block.get("type") == "text"
        ]
        prefix = "[TOOL ERROR]\n" if result.get("isError") else ""
        self.result_text.setPlainText(prefix + "\n".join(texts))
        self._render_content_extras(content)
        self.raw_text.setPlainText(json.dumps(result, indent=2, ensure_ascii=False))

    # ------------------------------------------------------------------
    # Rich content rendering (images / resources / unknown content types)
    # ------------------------------------------------------------------

    def _clear_result_extras(self) -> None:
        while self.result_extra_lay.count():
            item = self.result_extra_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _render_content_extras(self, content: List[Dict[str, Any]]) -> None:
        self._clear_result_extras()
        added = 0
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                continue  # already shown in result_text
            if block_type == "image":
                widget = self._make_image_widget(block)
            else:
                widget = self._make_json_widget(block)
            self.result_extra_lay.addWidget(widget)
            added += 1
        self.result_extra_lay.addStretch(1)
        self.result_extra_scroll.setVisible(added > 0)

    def _make_image_widget(self, block: Dict[str, Any]) -> QWidget:
        data = block.get("data")
        if not data:
            return self._make_json_widget(block)
        try:
            raw = base64.b64decode(data)
        except Exception:  # pylint: disable=broad-except
            return self._make_json_widget(block)
        pixmap = QPixmap()
        if not pixmap.loadFromData(raw):
            return self._make_json_widget(block)
        label = QLabel()
        label.setPixmap(pixmap)
        return label

    def _make_json_widget(self, block: Dict[str, Any]) -> QWidget:
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(json.dumps(block, indent=2, ensure_ascii=False))
        line_count = edit.toPlainText().count("\n") + 2
        edit.setFixedHeight(min(240, 22 * line_count))
        return edit

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

    def _on_error(self, failure: Any) -> None:
        self.connect_btn.setEnabled(True)
        self.refresh_btn.setEnabled(self.client is not None)
        self.call_btn.setEnabled(self.tool_list.currentItem() is not None)
        message = (
            failure.details() if isinstance(failure, MCPError) else str(failure)
        )
        if isinstance(failure, MCPError):
            self.status_label.setText(f"Error {failure.code}: {failure.message}")
            self.raw_text.setPlainText(message)
        else:
            self.status_label.setText("Error")
        QMessageBox.critical(self, "MCP Tester", message)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mcp-gui-tester",
        description="GUI tester for MCP servers over Streamable HTTP",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"MCP endpoint (default {DEFAULT_URL})")
    parser.add_argument(
        "--protocol",
        default="auto",
        choices=[mode for _label, mode in PROTOCOL_CHOICES],
        help=f"Protocol era to use (default auto; modern = {MODERN_PROTOCOL_VERSION})",
    )
    args = parser.parse_args()
    app = QApplication(sys.argv)
    win = MCPTesterWindow(args.url, protocol=args.protocol)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

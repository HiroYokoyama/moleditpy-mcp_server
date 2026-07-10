"""
Tests for mcp_gui_tester (mcp_gui_tester/src/mcp_gui_tester/app.py) —
the standalone MCP server tester GUI.

Three tiers:
1. Pure-function tests (_split_url, parse_json_param) — no Qt, no network.
2. MCPClient tests against a real in-process MCPHttpServer with a stub
   bridge — no Qt required (server.py is stdlib-only).
3. GUI tests using real PyQt6 offscreen — skipped automatically when
   PyQt6 is not installed (e.g. in the no-Qt CI job).
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTER_PATH = ROOT / "mcp_gui_tester" / "src" / "mcp_gui_tester" / "app.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# On Windows with both PyQt6 and PySide6 installed, the wrong Qt runtime can
# be found first, breaking the PyQt6 import with "DLL load failed". Point the
# DLL search at PyQt6's own Qt binaries before anything imports PyQt6.
if sys.platform == "win32" and importlib.util.find_spec("PyQt6") is not None:
    _spec = importlib.util.find_spec("PyQt6")
    if _spec is not None and _spec.submodule_search_locations:
        _qt_bin = Path(list(_spec.submodule_search_locations)[0]) / "Qt6" / "bin"
        if _qt_bin.is_dir():
            os.add_dll_directory(str(_qt_bin))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _has_pyqt6() -> bool:
    return importlib.util.find_spec("PyQt6") is not None


def _load_tester():
    """Import tools/mcp_tester.py as a module (requires real PyQt6)."""
    spec = importlib.util.spec_from_file_location("mcp_tester", TESTER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Tier 1: pure functions (need the module, hence PyQt6 for the import)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")
class TestPureHelpers:
    """_split_url and parse_json_param behave without any I/O."""

    @classmethod
    def setup_class(cls) -> None:
        cls.mod = _load_tester()

    def test_split_url_full(self) -> None:
        assert self.mod._split_url("http://localhost:9000/mcp") == (
            "localhost",
            9000,
            "/mcp",
        )

    def test_split_url_defaults(self) -> None:
        host, port, path = self.mod._split_url("http://127.0.0.1")
        assert (host, port, path) == ("127.0.0.1", 7891, "/mcp")

    def test_split_url_bare_host(self) -> None:
        host, port, path = self.mod._split_url("192.168.1.5:8080/rpc")
        assert (host, port, path) == ("192.168.1.5", 8080, "/rpc")

    def test_parse_json_object_ok(self) -> None:
        parsed = self.mod.parse_json_param("colors", "object", '{"0": "#FF0000"}')
        assert parsed == {"0": "#FF0000"}

    def test_parse_json_array_ok(self) -> None:
        assert self.mod.parse_json_param("ids", "array", "[1, 2, 3]") == [1, 2, 3]

    def test_parse_json_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            self.mod.parse_json_param("x", "object", "   ")

    def test_parse_json_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid JSON"):
            self.mod.parse_json_param("x", "object", "{nope}")

    def test_parse_json_wrong_container_raises(self) -> None:
        with pytest.raises(ValueError, match="must be a JSON array"):
            self.mod.parse_json_param("x", "array", '{"a": 1}')


# ---------------------------------------------------------------------------
# Tier 2: MCPClient against a real MCPHttpServer (stdlib-only, no Qt)
# ---------------------------------------------------------------------------


class _StubBridge:
    """Bridge stub answering the operations the tests exercise."""

    def call(self, operation: str, args: dict | None = None, timeout: float = 10.0):
        if operation == "get_app_info":
            return {
                "app": "MoleditPy-stub",
                "version": "0.0",
                "mcp_plugin_version": "test",
            }
        if operation == "get_molecule_info":
            return {"loaded": False}
        if operation == "load_smiles":
            assert args is not None and args["smiles"] == "CCO"
            return {}
        raise AssertionError(f"unexpected operation {operation!r}")


@pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")
class TestMCPClient:
    """The tester's JSON-RPC client speaks the server's transport correctly."""

    @classmethod
    def setup_class(cls) -> None:
        from mcp_server.server import MCPHttpServer, _TOOLS

        cls.mod = _load_tester()
        cls.n_tools = len(_TOOLS)
        cls.port = _free_port()
        cls.server = MCPHttpServer(_StubBridge(), "Stub MCP", "0.0", port=cls.port)
        cls.server.start()
        time.sleep(0.2)
        cls.client = cls.mod.MCPClient(f"http://127.0.0.1:{cls.port}/mcp")

    @classmethod
    def teardown_class(cls) -> None:
        cls.server.stop()

    def test_initialize(self) -> None:
        info = self.client.initialize()
        assert info["serverInfo"]["name"] == "Stub MCP"

    def test_list_tools(self) -> None:
        tools = self.client.list_tools()
        assert len(tools) == self.n_tools
        assert all("name" in t and "inputSchema" in t for t in tools)

    def test_call_tool_success(self) -> None:
        result = self.client.call_tool("get_app_info", {})
        assert not result.get("isError")
        assert "MoleditPy-stub" in result["content"][0]["text"]

    def test_call_tool_missing_required_arg(self) -> None:
        result = self.client.call_tool("load_molecule_from_smiles", {})
        assert result.get("isError")

    def test_call_tool_with_args(self) -> None:
        result = self.client.call_tool(
            "load_molecule_from_smiles", {"smiles": "CCO"}
        )
        assert not result.get("isError")

    def test_call_unknown_tool(self) -> None:
        result = self.client.call_tool("no_such_tool", {})
        assert result.get("isError")

    def test_unknown_method_raises(self) -> None:
        with pytest.raises(RuntimeError):
            self.client._rpc("bogus/method")


# ---------------------------------------------------------------------------
# Tier 3: GUI (real PyQt6, offscreen)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")
class TestTesterGUI:
    """Window construction, schema-driven form generation, and a live call."""

    @classmethod
    def setup_class(cls) -> None:
        from PyQt6.QtWidgets import QApplication

        from mcp_server.server import MCPHttpServer

        cls.mod = _load_tester()
        cls.app = QApplication.instance() or QApplication([])
        cls.port = _free_port()
        cls.server = MCPHttpServer(_StubBridge(), "Stub MCP", "0.0", port=cls.port)
        cls.server.start()
        time.sleep(0.2)
        cls.client = cls.mod.MCPClient(f"http://127.0.0.1:{cls.port}/mcp")
        cls.tools = cls.client.list_tools()

    @classmethod
    def teardown_class(cls) -> None:
        cls.server.stop()

    def _make_window(self):
        win = self.mod.MCPTesterWindow(f"http://127.0.0.1:{self.port}/mcp")
        win.tools = self.tools
        win._populate_tool_list()
        return win

    def test_url_fields_parsed(self) -> None:
        win = self._make_window()
        assert win.host_edit.text() == "127.0.0.1"
        assert win.port_spin.value() == self.port
        assert win.path_edit.text() == "/mcp"

    def test_form_generation_for_all_tools(self) -> None:
        win = self._make_window()
        assert win.tool_list.count() == len(self.tools)
        for i in range(win.tool_list.count()):
            win.tool_list.setCurrentRow(i)
            self.app.processEvents()
        assert win.call_btn.isEnabled()

    def test_required_and_optional_args(self) -> None:
        win = self._make_window()
        names = [t["name"] for t in self.tools]
        win.tool_list.setCurrentRow(names.index("write_text_file"))
        self.app.processEvents()
        by_name = {f.name: f for f in win.fields}
        by_name["path"].widget.setText("a.txt")
        by_name["content"].widget.setPlainText("hello")
        assert by_name["overwrite"].include_box is not None
        args = {f.name: f.value() for f in win.fields if f.is_included()}
        assert args == {"path": "a.txt", "content": "hello"}
        by_name["overwrite"].include_box.setChecked(True)
        by_name["overwrite"].widget.setChecked(True)
        args = {f.name: f.value() for f in win.fields if f.is_included()}
        assert args["overwrite"] is True

    def test_json_object_param_validation(self) -> None:
        win = self._make_window()
        names = [t["name"] for t in self.tools]
        win.tool_list.setCurrentRow(names.index("highlight_atoms"))
        self.app.processEvents()
        field = win.fields[0]
        field.widget.setPlainText('{"0": "#FF0000"}')
        assert field.value() == {"0": "#FF0000"}
        field.widget.setPlainText("not json")
        with pytest.raises(ValueError):
            field.value()

    def test_tool_filter(self) -> None:
        win = self._make_window()
        win.filter_edit.setText("get_app_info")
        visible = [
            win.tool_list.item(i).text()
            for i in range(win.tool_list.count())
            if not win.tool_list.item(i).isHidden()
        ]
        assert "get_app_info" in visible
        assert len(visible) < len(self.tools)

    def test_end_to_end_call(self) -> None:
        win = self._make_window()
        win.client = self.client  # normally set by the Connect button
        names = [t["name"] for t in self.tools]
        win.tool_list.setCurrentRow(names.index("get_app_info"))
        self.app.processEvents()
        win._on_call()
        deadline = time.time() + 5
        while time.time() < deadline and not win.result_text.toPlainText():
            self.app.processEvents()
            time.sleep(0.02)
        assert "MoleditPy-stub" in win.result_text.toPlainText()
        assert '"content"' in win.raw_text.toPlainText()

    def test_param_defaults_and_enum(self) -> None:
        schema = {
            "type": "string",
            "enum": ["fast", "slow"],
            "default": "slow",
        }
        field = self.mod.ParamField("mode", schema, required=True)
        assert field.value() == "slow"
        field = self.mod.ParamField(
            "count", {"type": "integer", "default": 5}, required=True
        )
        assert field.value() == 5


@pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")
class TestUnionParamField:
    """string-or-array (oneOf) fields parse JSON arrays, pass strings through."""

    @classmethod
    def setup_class(cls) -> None:
        from PyQt6.QtWidgets import QApplication

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])
        cls.mod = _load_tester()
        cls.union_schema = {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
            "description": "text or lines",
        }

    def _field(self):
        return self.mod.ParamField("header", self.union_schema, required=False)

    def test_union_field_uses_multiline_editor(self) -> None:
        from PyQt6.QtWidgets import QPlainTextEdit

        assert isinstance(self._field().widget, QPlainTextEdit)

    def test_union_field_parses_json_array(self) -> None:
        field = self._field()
        field.widget.setPlainText('["line1", "line2"]')
        assert field.value() == ["line1", "line2"]

    def test_union_field_passes_plain_text_through(self) -> None:
        field = self._field()
        field.widget.setPlainText("! Opt\n* xyz 0 1")
        assert field.value() == "! Opt\n* xyz 0 1"

    def test_union_field_invalid_json_stays_string(self) -> None:
        field = self._field()
        field.widget.setPlainText('["broken')
        assert field.value() == '["broken'

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

    def test_custom_headers_sent_on_every_request(self) -> None:
        client = self.mod.MCPClient(
            f"http://127.0.0.1:{self.port}/mcp", headers={"X-Test": "yes"}
        )
        captured = []
        real_urlopen = self.mod.urllib.request.urlopen

        def _capturing_urlopen(req, *args, **kwargs):
            captured.append(dict(req.header_items()))
            return real_urlopen(req, *args, **kwargs)

        self.mod.urllib.request.urlopen = _capturing_urlopen
        try:
            client.initialize()
        finally:
            self.mod.urllib.request.urlopen = real_urlopen
        assert captured
        assert captured[0].get("X-test") == "yes"

    def test_no_headers_means_no_extra_headers(self) -> None:
        client = self.mod.MCPClient(f"http://127.0.0.1:{self.port}/mcp")
        assert client.headers == {}


# ---------------------------------------------------------------------------
# Tier 3: GUI (real PyQt6, offscreen)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")
class TestTesterGUI:
    """Window construction, schema-driven form generation, and a live call."""

    @classmethod
    def setup_class(cls) -> None:
        import tempfile

        from PyQt6.QtWidgets import QApplication

        from mcp_server.server import MCPHttpServer

        cls.mod = _load_tester()
        # Never touch the real user's home directory during tests.
        cls._history_tmpdir = tempfile.mkdtemp(prefix="mcp_gui_tester_history_")
        cls.mod.HISTORY_PATH = Path(cls._history_tmpdir) / "history.json"
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
        import shutil

        shutil.rmtree(cls._history_tmpdir, ignore_errors=True)

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
        win.tool_list.setCurrentRow(names.index("set_cpk_color_override"))
        self.app.processEvents()
        field = win.fields[0]
        field.widget.setPlainText('{"0": "#FF0000"}')
        assert field.value() == {"0": "#FF0000"}
        field.widget.setPlainText("not json")
        with pytest.raises(ValueError):
            field.value()

    def test_refresh_tools_button_preserves_selection(self) -> None:
        win = self._make_window()
        win.client = self.client  # normally set by the Connect button
        names = [t["name"] for t in self.tools]
        win.tool_list.setCurrentRow(names.index("get_app_info"))
        self.app.processEvents()
        assert win.refresh_btn is not None
        win._on_refresh_tools()
        deadline = time.time() + 5
        while time.time() < deadline and not win.refresh_btn.isEnabled():
            self.app.processEvents()
            time.sleep(0.02)
        current = win.tool_list.currentItem()
        assert current is not None
        assert current.data(self.mod.Qt.ItemDataRole.UserRole)["name"] == "get_app_info"
        assert win.tool_list.count() == len(self.tools)

    def test_refresh_tools_noop_without_client(self) -> None:
        win = self._make_window()
        win.client = None
        win._on_refresh_tools()  # must not raise

    def test_headers_field_rejects_invalid_json(self, monkeypatch) -> None:
        # QMessageBox.warning() opens a real modal event loop even under the
        # offscreen platform, so stub it out to keep the test non-blocking.
        monkeypatch.setattr(self.mod.QMessageBox, "warning", lambda *a, **k: None)
        win = self._make_window()
        win.headers_edit.setText("{not json}")
        win._on_connect()
        # invalid JSON must not create a client
        assert win.client is None

    def test_headers_field_accepts_valid_json_object(self) -> None:
        win = self._make_window()
        win.headers_edit.setText('{"Authorization": "Bearer xyz"}')
        win._on_connect()
        assert win.client is not None
        assert win.client.headers == {"Authorization": "Bearer xyz"}

    def test_headers_field_rejects_non_object_json(self, monkeypatch) -> None:
        monkeypatch.setattr(self.mod.QMessageBox, "warning", lambda *a, **k: None)
        win = self._make_window()
        win.headers_edit.setText("[1, 2, 3]")
        win._on_connect()
        assert win.client is None

    def test_reset_form_clears_prefill(self) -> None:
        win = self._make_window()
        win.history.data["write_text_file"] = {"path": "remembered.txt"}
        names = [t["name"] for t in self.tools]
        win.tool_list.setCurrentRow(names.index("write_text_file"))
        self.app.processEvents()
        by_name = {f.name: f for f in win.fields}
        assert by_name["path"].widget.text() == "remembered.txt"
        win._on_reset_form()
        by_name = {f.name: f for f in win.fields}
        assert by_name["path"].widget.text() == ""

    def test_argument_history_prefills_form_and_records_on_call(self) -> None:
        win = self._make_window()
        win.client = self.client
        names = [t["name"] for t in self.tools]
        win.tool_list.setCurrentRow(names.index("get_app_info"))
        self.app.processEvents()
        win._on_call()
        deadline = time.time() + 5
        while time.time() < deadline and not win.result_text.toPlainText():
            self.app.processEvents()
            time.sleep(0.02)
        assert win.history.get("get_app_info") == {}

    def test_response_rendering_image_and_resource_content(self) -> None:
        import base64

        from PyQt6.QtWidgets import QLabel, QPlainTextEdit

        win = self._make_window()
        png_1x1 = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YA"
            "AAAASUVORK5CYII="
        )
        content = [
            {"type": "text", "text": "hello"},
            {"type": "image", "data": base64.b64encode(png_1x1).decode("ascii"), "mimeType": "image/png"},
            {"type": "resource", "resource": {"uri": "file:///x.txt"}},
            {"type": "something_else", "value": 42},
        ]
        win._render_content_extras(content)
        widgets = [
            win.result_extra_lay.itemAt(i).widget()
            for i in range(win.result_extra_lay.count())
            if win.result_extra_lay.itemAt(i).widget() is not None
        ]
        # text block contributes nothing to result_extra; image + resource + unknown do
        assert len(widgets) == 3
        assert isinstance(widgets[0], QLabel) and not widgets[0].pixmap().isNull()
        assert isinstance(widgets[1], QPlainTextEdit)
        assert "resource" in widgets[1].toPlainText()
        assert isinstance(widgets[2], QPlainTextEdit)
        assert "something_else" in widgets[2].toPlainText()

    def test_response_rendering_bad_image_data_falls_back_to_json(self) -> None:
        from PyQt6.QtWidgets import QPlainTextEdit

        win = self._make_window()
        content = [{"type": "image", "data": "not-base64!!", "mimeType": "image/png"}]
        win._render_content_extras(content)
        widget = win.result_extra_lay.itemAt(0).widget()
        assert isinstance(widget, QPlainTextEdit)

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


@pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")
class TestGeneralizedOneOf:
    """Generalized oneOf handling beyond the string-or-array special case."""

    @classmethod
    def setup_class(cls) -> None:
        from PyQt6.QtWidgets import QApplication

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])
        cls.mod = _load_tester()

    def test_string_or_object_uses_multiline_json_editor(self) -> None:
        from PyQt6.QtWidgets import QPlainTextEdit

        schema = {"oneOf": [{"type": "string"}, {"type": "object"}]}
        field = self.mod.ParamField("meta", schema, required=False)
        assert isinstance(field.widget, QPlainTextEdit)
        field.widget.setPlainText('{"a": 1}')
        assert field.value() == {"a": 1}
        field.widget.setPlainText("plain text")
        assert field.value() == "plain text"

    def test_integer_or_boolean_falls_back_to_first_alternative_widget(self) -> None:
        from PyQt6.QtWidgets import QSpinBox

        schema = {"oneOf": [{"type": "integer"}, {"type": "boolean"}]}
        field = self.mod.ParamField("level", schema, required=False)
        assert isinstance(field.widget, QSpinBox)

    def test_boolean_or_string_prefers_string_editor(self) -> None:
        # Per the widget-selection rule (array/object > string > fallback),
        # a boolean+string union still gets a text editor, not a checkbox.
        from PyQt6.QtWidgets import QLineEdit

        schema = {"oneOf": [{"type": "boolean"}, {"type": "string"}]}
        field = self.mod.ParamField("flag", schema, required=False)
        assert isinstance(field.widget, QLineEdit)
        field.widget.setText("true")
        assert field.value() is True

    def test_boolean_only_oneof_falls_back_to_checkbox(self) -> None:
        from PyQt6.QtWidgets import QCheckBox

        schema = {"oneOf": [{"type": "boolean"}, {"type": "integer"}]}
        field = self.mod.ParamField("flag", schema, required=False)
        assert isinstance(field.widget, QCheckBox)
        field.widget.setChecked(True)
        assert field.value() is True

    def test_number_or_integer_falls_back_to_double_spinbox(self) -> None:
        from PyQt6.QtWidgets import QDoubleSpinBox

        schema = {"oneOf": [{"type": "number"}, {"type": "integer"}]}
        field = self.mod.ParamField("weight", schema, required=False)
        # no array/object/string alt present -> first alt ("number") wins.
        assert isinstance(field.widget, QDoubleSpinBox)
        field.widget.setValue(3.5)
        assert field.value() == 3.5

    def test_coerce_union_value_prefers_matching_union_type(self) -> None:
        # Exercise _coerce_union_value directly for a string+integer union
        # backed by a line edit, since building the widget always prefers
        # the "string" branch when present.
        schema = {"oneOf": [{"type": "integer"}, {"type": "string"}]}
        field = self.mod.ParamField("count_or_label", schema, required=False)
        assert field._coerce_union_value("42") == 42
        assert field._coerce_union_value("not a number") == "not a number"
        # "boolean" is not an allowed alternative, so JSON-looking "true"
        # stays a raw string rather than becoming a Python bool.
        assert field._coerce_union_value("true") == "true"

    def test_explicit_type_is_not_treated_as_oneof(self) -> None:
        # A schema with an explicit top-level "type" alongside oneOf/enum-like
        # constructs must not go through the union widget path.
        schema = {"type": "string", "oneOf": [{"const": "a"}, {"const": "b"}]}
        field = self.mod.ParamField("choice", schema, required=False)
        from PyQt6.QtWidgets import QLineEdit

        assert isinstance(field.widget, QLineEdit)


@pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")
class TestArgumentHistory:
    """Per-tool argument memory: in-memory + persisted JSON file."""

    @classmethod
    def setup_class(cls) -> None:
        cls.mod = _load_tester()

    def test_remember_and_get_roundtrip(self, tmp_path) -> None:
        history = self.mod.ArgumentHistory(path=tmp_path / "history.json")
        history.remember("tool_a", {"x": 1})
        assert history.get("tool_a") == {"x": 1}
        assert history.get("tool_b") is None

    def test_history_persists_to_file(self, tmp_path) -> None:
        path = tmp_path / "history.json"
        history = self.mod.ArgumentHistory(path=path)
        history.remember("tool_a", {"x": 1})
        assert path.is_file()
        reloaded = self.mod.ArgumentHistory(path=path)
        assert reloaded.get("tool_a") == {"x": 1}

    def test_corrupt_history_file_ignored_silently(self, tmp_path) -> None:
        path = tmp_path / "history.json"
        path.write_text("{not valid json", encoding="utf-8")
        history = self.mod.ArgumentHistory(path=path)  # must not raise
        assert history.get("anything") is None

    def test_unreadable_history_file_ignored_silently(self, tmp_path) -> None:
        path = tmp_path / "missing_dir" / "history.json"  # parent doesn't exist
        history = self.mod.ArgumentHistory(path=path)  # must not raise on load
        assert history.get("anything") is None
        history.remember("tool_a", {"x": 1})  # save failure must not raise either
        assert history.get("tool_a") == {"x": 1}

    def test_capped_at_max_tools(self, tmp_path) -> None:
        path = tmp_path / "history.json"
        history = self.mod.ArgumentHistory(path=path, max_tools=3)
        for i in range(5):
            history.remember(f"tool_{i}", {"i": i})
        assert len(history.data) == 3
        # oldest entries evicted first
        assert history.get("tool_0") is None
        assert history.get("tool_1") is None
        assert history.get("tool_4") == {"i": 4}

    def test_remember_moves_tool_to_most_recent(self, tmp_path) -> None:
        path = tmp_path / "history.json"
        history = self.mod.ArgumentHistory(path=path, max_tools=2)
        history.remember("tool_a", {"x": 1})
        history.remember("tool_b", {"x": 2})
        history.remember("tool_a", {"x": 3})  # re-remember a -> now most recent
        history.remember("tool_c", {"x": 4})  # should evict tool_b, not tool_a
        assert history.get("tool_b") is None
        assert history.get("tool_a") == {"x": 3}
        assert history.get("tool_c") == {"x": 4}

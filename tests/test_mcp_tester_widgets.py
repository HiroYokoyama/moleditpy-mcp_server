"""
Coverage for mcp_gui_tester's schema-driven widget building, value coercion,
history prefill, result rendering, and the argparse entry point.

Real PyQt6 offscreen; skipped when PyQt6 is unavailable.
"""

from __future__ import annotations

import base64
import json

import pytest

from test_mcp_tester import _has_pyqt6, _load_tester

pytestmark = pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def mod(app):
    return _load_tester()


def _field(mod, name, schema, required=False):
    return mod.ParamField(name, schema, required)


# ---------------------------------------------------------------------------
# Widget selection per schema type
# ---------------------------------------------------------------------------


def test_boolean_widget_uses_default(mod):
    field = _field(mod, "flag", {"type": "boolean", "default": True})
    assert field.widget.isChecked() is True
    assert field.value() is True


def test_integer_widget_uses_default(mod):
    field = _field(mod, "n", {"type": "integer", "default": 7})
    assert field.value() == 7


def test_number_widget_uses_default(mod):
    field = _field(mod, "x", {"type": "number", "default": 1.5})
    assert field.value() == pytest.approx(1.5)


def test_enum_widget_uses_default(mod):
    field = _field(mod, "style", {"enum": ["a", "b"], "default": "b"})
    assert field.value() == "b"


def test_array_widget_parses_json(mod):
    field = _field(mod, "items", {"type": "array"})
    field.widget.setPlainText("[1, 2]")
    assert field.value() == [1, 2]


def test_multiline_hint_widget_takes_default_text(mod):
    field = _field(mod, "code", {"type": "string", "default": "print(1)"})
    assert field.value() == "print(1)"


def test_plain_string_widget_takes_default(mod):
    field = _field(mod, "path", {"type": "string", "default": "a.txt"})
    assert field.value() == "a.txt"


# ---------------------------------------------------------------------------
# oneOf unions
# ---------------------------------------------------------------------------


def test_oneof_string_or_array_prefills_string_default(mod):
    schema = {"oneOf": [{"type": "string"}, {"type": "array"}], "default": "hi"}
    field = _field(mod, "content", schema)
    assert field.value() == "hi"


def test_oneof_object_alternative_gets_json_editor(mod):
    schema = {"oneOf": [{"type": "string"}, {"type": "object"}]}
    field = _field(mod, "payload", schema)
    field.widget.setPlainText('{"a": 1}')
    assert field.value() == {"a": 1}


def test_oneof_string_only_multiline_hint(mod):
    schema = {"oneOf": [{"type": "string"}], "default": "x"}
    field = _field(mod, "code", schema)
    assert field.value() == "x"


def test_oneof_string_only_single_line(mod):
    schema = {"oneOf": [{"type": "string"}], "default": "y"}
    field = _field(mod, "title", schema)
    assert field.value() == "y"


def test_oneof_boolean_fallback(mod):
    schema = {"oneOf": [{"type": "boolean"}, {"type": "null"}], "default": True}
    assert _field(mod, "flag", schema).value() is True


def test_oneof_integer_fallback(mod):
    schema = {"oneOf": [{"type": "integer"}, {"type": "null"}], "default": 3}
    assert _field(mod, "n", schema).value() == 3


def test_oneof_number_fallback(mod):
    schema = {"oneOf": [{"type": "number"}, {"type": "null"}], "default": 2.5}
    assert _field(mod, "x", schema).value() == pytest.approx(2.5)


def test_oneof_without_usable_type_falls_back_to_line_edit(mod):
    schema = {"oneOf": [{"const": 1}], "default": "raw"}
    assert _field(mod, "weird", schema).value() == "raw"


def test_oneof_coerces_json_array_text(mod):
    schema = {"oneOf": [{"type": "string"}, {"type": "array"}]}
    field = _field(mod, "content", schema)
    field.widget.setPlainText('["a", "b"]')
    assert field.value() == ["a", "b"]


def test_oneof_keeps_plain_text_as_string(mod):
    schema = {"oneOf": [{"type": "string"}, {"type": "array"}]}
    field = _field(mod, "content", schema)
    field.widget.setPlainText("just text")
    assert field.value() == "just text"


def test_oneof_rejects_type_outside_the_union(mod):
    schema = {"oneOf": [{"type": "string"}, {"type": "array"}]}
    field = _field(mod, "content", schema)
    field.widget.setPlainText("{}")  # object is not an alternative
    assert field.value() == "{}"


def test_oneof_accepts_integer_for_number_alternative(mod):
    schema = {"oneOf": [{"type": "number"}, {"type": "string"}]}
    field = _field(mod, "x", schema)
    field.widget.setText("42")
    assert field.value() == 42


def test_oneof_invalid_json_stays_text(mod):
    schema = {"oneOf": [{"type": "string"}, {"type": "array"}]}
    field = _field(mod, "content", schema)
    field.widget.setPlainText("[1, 2")  # looks like JSON, does not parse
    assert field.value() == "[1, 2"


def test_looks_like_json_covers_scalars(mod):
    assert mod._looks_like_json("true")
    assert mod._looks_like_json("1.5")
    assert not mod._looks_like_json("")
    assert not mod._looks_like_json("hello")


# ---------------------------------------------------------------------------
# set_value (history prefill)
# ---------------------------------------------------------------------------


def test_set_value_on_each_widget_kind(mod):
    combo = _field(mod, "style", {"enum": ["a", "b"]})
    combo.set_value("b")
    assert combo.value() == "b"

    check = _field(mod, "flag", {"type": "boolean"})
    check.set_value(True)
    assert check.value() is True

    spin = _field(mod, "n", {"type": "integer"})
    spin.set_value(5)
    assert spin.value() == 5

    dspin = _field(mod, "x", {"type": "number"})
    dspin.set_value(2.25)
    assert dspin.value() == pytest.approx(2.25)

    text = _field(mod, "path", {"type": "string"})
    text.set_value("a.txt")
    assert text.value() == "a.txt"


def test_set_value_ignores_uncastable_numbers(mod):
    spin = _field(mod, "n", {"type": "integer"})
    spin.set_value("not a number")
    assert spin.value() == 0
    dspin = _field(mod, "x", {"type": "number"})
    dspin.set_value("not a number")
    assert dspin.value() == pytest.approx(0.0)


def test_set_value_serializes_containers_into_json_editor(mod):
    field = _field(mod, "items", {"type": "array"})
    field.set_value([1, 2])
    assert field.value() == [1, 2]


def test_set_value_checks_the_include_box(mod):
    from PyQt6.QtWidgets import QCheckBox

    field = _field(mod, "path", {"type": "string"})
    field.include_box = QCheckBox()
    assert field.is_included() is False
    field.set_value("a.txt")
    assert field.is_included() is True


# ---------------------------------------------------------------------------
# Window paths that need no server
# ---------------------------------------------------------------------------


@pytest.fixture()
def win(mod):
    return mod.MCPTesterWindow("http://127.0.0.1:1/mcp")


def test_connect_without_host_warns_and_does_nothing(mod, win, monkeypatch):
    warned = []
    monkeypatch.setattr(
        mod.QMessageBox, "warning", lambda *a, **k: warned.append(a[2])
    )
    win.host_edit.setText("   ")
    win._on_connect()
    assert warned and win.client is None


def test_connect_rejects_non_object_headers(mod, win, monkeypatch):
    warned = []
    monkeypatch.setattr(
        mod.QMessageBox, "warning", lambda *a, **k: warned.append(a[2])
    )
    win.headers_edit.setText("[1, 2]")
    win._on_connect()
    assert warned and win.client is None


def test_connect_normalizes_a_path_without_leading_slash(mod, win):
    win.path_edit.setText("mcp")
    win._on_connect()
    assert win.client.url.endswith("/mcp")


def test_tool_selection_cleared_disables_buttons(mod, win):
    win._on_tool_selected(None, None)
    assert win.call_btn.isEnabled() is False
    assert win.reset_form_btn.isEnabled() is False


def test_reset_form_without_selection_is_noop(mod, win):
    win._on_reset_form()  # must not raise


def test_call_without_selection_is_noop(mod, win):
    win.client = object()
    win._on_call()  # no current item -> returns early


def test_call_warns_on_invalid_json_argument(mod, win, monkeypatch):
    warned = []
    monkeypatch.setattr(
        mod.QMessageBox, "warning", lambda *a, **k: warned.append(a[2])
    )
    tool = {
        "name": "t",
        "description": "",
        "inputSchema": {
            "type": "object",
            "properties": {"items": {"type": "array"}},
            "required": ["items"],
        },
    }
    win.tools = [tool]
    win._populate_tool_list()
    win.tool_list.setCurrentRow(0)
    win.client = object()
    win.fields[0].widget.setPlainText("not json")
    win._on_call()
    assert warned and "invalid JSON" in warned[0]


def test_form_shows_placeholder_when_tool_has_no_parameters(mod, win):
    win.tools = [{"name": "t", "description": "", "inputSchema": {"type": "object"}}]
    win._populate_tool_list()
    win.tool_list.setCurrentRow(0)
    assert win.call_btn.isEnabled()
    assert win.fields == []


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------


def _png_bytes():
    from PyQt6.QtGui import QImage

    from PyQt6.QtCore import QBuffer, QByteArray

    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(0)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return bytes(data)


def test_image_block_is_rendered_inline(mod, win):
    block = {"type": "image", "data": base64.b64encode(_png_bytes()).decode()}
    win._render_content_extras([{"type": "text", "text": "t"}, block])
    assert win.result_extra_scroll.isVisible() or win.result_extra_lay.count() > 1


def test_image_block_without_data_falls_back_to_json(mod, win):
    widget = win._make_image_widget({"type": "image"})
    assert "image" in widget.toPlainText()


def test_image_block_with_bad_base64_falls_back_to_json(mod, win):
    widget = win._make_image_widget({"type": "image", "data": "!!!"})
    assert "image" in widget.toPlainText()


def test_undecodable_image_payload_falls_back_to_json(mod, win):
    payload = base64.b64encode(b"not really an image").decode()
    widget = win._make_image_widget({"type": "image", "data": payload})
    assert "image" in widget.toPlainText()


def test_clearing_extras_removes_previous_widgets(mod, win):
    win._render_content_extras([{"type": "resource", "uri": "file:///x"}])
    assert win.result_extra_lay.count() > 0
    win._clear_result_extras()
    assert win.result_extra_lay.count() == 0


def test_call_done_flags_tool_errors(mod, win):
    win._on_call_done({"isError": True, "content": [{"type": "text", "text": "bad"}]})
    assert win.result_text.toPlainText().startswith("[TOOL ERROR]")
    assert json.loads(win.raw_text.toPlainText())["isError"] is True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def test_main_parses_arguments_and_launches(mod, monkeypatch):
    created = {}

    class _FakeApp:
        def __init__(self, argv):
            pass

        def exec(self):
            return 0

    class _FakeWindow:
        def __init__(self, url, protocol="auto"):
            created["url"] = url
            created["protocol"] = protocol

        def show(self):
            created["shown"] = True

    monkeypatch.setattr(mod, "QApplication", _FakeApp)
    monkeypatch.setattr(mod, "MCPTesterWindow", _FakeWindow)
    monkeypatch.setattr(
        mod.sys, "argv", ["mcp-gui-tester", "--url", "http://x/mcp", "--protocol", "modern"]
    )
    with pytest.raises(SystemExit) as excinfo:
        mod.main()
    assert excinfo.value.code == 0
    assert created == {"url": "http://x/mcp", "protocol": "modern", "shown": True}


def test_set_value_writes_plain_text_into_a_json_editor(mod):
    field = _field(mod, "items", {"type": "array"})
    field.set_value("raw text")
    assert field.widget.toPlainText() == "raw text"


def test_package_exports_match_the_module(app):
    """The installed package (not the by-path import) exposes the API."""
    import importlib
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[1] / "mcp_gui_tester" / "src")
    sys.path.insert(0, src)
    try:
        pkg = importlib.import_module("mcp_gui_tester")
        importlib.import_module("mcp_gui_tester.__main__")
        assert pkg.__version__
        assert pkg.MCPClient and pkg.MCPTesterWindow and pkg.main
    finally:
        sys.path.remove(src)
        for name in [k for k in sys.modules if k.startswith("mcp_gui_tester")]:
            del sys.modules[name]

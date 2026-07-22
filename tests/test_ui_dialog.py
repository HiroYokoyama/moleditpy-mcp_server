"""
Tests for mcp_server/ui.py that genuinely import the module.

Unlike test_ui.py (which extracts method source via ast + exec against a
fake ``self`` because the blanket MagicMock PyQt6 mock in conftest.py makes
QDialog un-subclassable), this file installs its own rich, real
(subclassable) PyQt6 stand-ins from ui_qt_stubs.py *before* importing
mcp_server.ui, so the module is actually imported and its statements are
executed and counted toward coverage.

The stub install/removal is scoped to this module via a fixture that
snapshots and restores sys.modules, so it never leaks into other test
files that rely on tests/conftest.py's MagicMock-based mock.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from ui_qt_stubs import (
    QApplication,
    QFileDialog,
    install_ui_qt_stubs,
    remove_ui_qt_stubs,
)


@pytest.fixture()
def ui_module():
    """Install rich Qt stubs, freshly import mcp_server.ui, then clean up."""
    saved = {
        k: v
        for k, v in sys.modules.items()
        if k.startswith("PyQt6") or k == "mcp_server.ui"
    }
    install_ui_qt_stubs()
    try:
        import mcp_server.ui as mod  # noqa: PLC0415 - intentional fresh import

        yield mod
    finally:
        remove_ui_qt_stubs()
        for k in list(sys.modules):
            if k.startswith("PyQt6") or k == "mcp_server.ui":
                del sys.modules[k]
        sys.modules.update(saved)


def make_plugin(*, running=False, url="http://127.0.0.1:7891/mcp", settings=None):
    settings = dict(settings or {})
    plugin = MagicMock()
    plugin.is_running = running
    plugin.url = url
    plugin.context.get_main_window.return_value = None
    plugin.context.get_setting.side_effect = lambda key, default=None: settings.get(
        key, default
    )
    plugin.context.set_setting.side_effect = lambda key, value: settings.__setitem__(
        key, value
    )
    return plugin, settings


# ---------------------------------------------------------------------------
# render_client_config (module-level function, real import)
# ---------------------------------------------------------------------------


def test_render_client_config_real_import(ui_module):
    snippet = ui_module.render_client_config("Claude Desktop", 9999)
    assert "9999" in snippet
    assert "{PORT}" not in snippet


# ---------------------------------------------------------------------------
# Dialog construction / refresh
# ---------------------------------------------------------------------------


def test_dialog_builds_and_refreshes_stopped(ui_module):
    plugin, _ = make_plugin(running=False, url="http://127.0.0.1:7891/mcp")
    dlg = ui_module.MCPStatusDialog(plugin)
    assert "Stopped" in dlg._status_lbl.text()
    assert dlg._toggle_btn.text() == "Start Server"
    assert dlg._port_spin.isEnabled()
    assert dlg._url_lbl.text() == "http://127.0.0.1:7891/mcp"


def test_dialog_refresh_running(ui_module):
    plugin, _ = make_plugin(running=True, url="http://127.0.0.1:8888/mcp")
    dlg = ui_module.MCPStatusDialog(plugin)
    assert "Running" in dlg._status_lbl.text()
    assert dlg._toggle_btn.text() == "Stop Server"
    assert not dlg._port_spin.isEnabled()


def test_dialog_uses_saved_settings(ui_module, tmp_path):
    plugin, _ = make_plugin(
        settings={
            "port": 12345,
            "auto_start": True,
            "file_io_base_dir": str(tmp_path),
        }
    )
    dlg = ui_module.MCPStatusDialog(plugin)
    assert dlg._port_spin.value() == 12345
    assert dlg._auto_start_chk.isChecked()
    assert dlg._base_dir_edit.text() == str(tmp_path)


# ---------------------------------------------------------------------------
# Client config view
# ---------------------------------------------------------------------------


def test_update_config_view_and_client_change(ui_module):
    plugin, _ = make_plugin()
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._client_combo.setCurrentText("Cursor")
    expected = ui_module.render_client_config("Cursor", dlg._port_spin.value())
    assert dlg._config_view.toPlainText() == expected
    assert dlg._config_note.text()  # non-empty note


def test_update_config_view_unknown_client_is_noop(ui_module):
    plugin, _ = make_plugin()
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._config_view.setPlainText("unchanged")
    dlg._client_combo._items.append("Not A Real Client")
    dlg._client_combo._current = len(dlg._client_combo._items) - 1
    dlg._update_config_view()
    assert dlg._config_view.toPlainText() == "unchanged"


def test_port_change_updates_config_view(ui_module):
    plugin, _ = make_plugin()
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._port_spin.setValue(55555)
    dlg._port_spin.valueChanged.emit(55555)
    assert "55555" in dlg._config_view.toPlainText()


# ---------------------------------------------------------------------------
# _copy_config / _copy_url
# ---------------------------------------------------------------------------


def test_copy_config_sets_clipboard_and_status(ui_module):
    plugin, _ = make_plugin()
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._copy_config()
    assert QApplication.clipboard().text_set == dlg._config_view.toPlainText()
    plugin.context.show_status_message.assert_any_call(
        "Client configuration copied to clipboard.", 2000
    )


def test_copy_url_sets_clipboard_and_status(ui_module):
    plugin, _ = make_plugin(url="http://127.0.0.1:7000/mcp")
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._copy_url()
    assert QApplication.clipboard().text_set == "http://127.0.0.1:7000/mcp"
    plugin.context.show_status_message.assert_any_call(
        "MCP server URL copied to clipboard.", 2000
    )


# ---------------------------------------------------------------------------
# _toggle
# ---------------------------------------------------------------------------


def test_toggle_starts_when_stopped(ui_module):
    plugin, settings = make_plugin(running=False)
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._port_spin.setValue(9001)
    dlg._toggle()
    plugin.start.assert_called_once_with(port=9001)
    assert settings["port"] == 9001


def test_toggle_stops_when_running(ui_module):
    plugin, _ = make_plugin(running=True)
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._toggle()
    plugin.stop.assert_called_once()


# ---------------------------------------------------------------------------
# _on_auto_start_toggled
# ---------------------------------------------------------------------------


def test_auto_start_toggle_persists_setting(ui_module):
    plugin, settings = make_plugin()
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._auto_start_chk.setChecked(True)
    assert settings["auto_start"] is True


# ---------------------------------------------------------------------------
# _on_base_dir_changed
# ---------------------------------------------------------------------------


def test_base_dir_changed_empty_clears_setting(ui_module):
    plugin, settings = make_plugin(settings={"file_io_base_dir": "/prev"})
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._base_dir_edit.setText("")
    dlg._on_base_dir_changed()
    assert settings["file_io_base_dir"] is None


def test_base_dir_changed_whitespace_clears_setting(ui_module):
    plugin, settings = make_plugin()
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._base_dir_edit.setText("   ")
    dlg._on_base_dir_changed()
    assert settings["file_io_base_dir"] is None


def test_base_dir_changed_valid_dir_resolves_and_saves(ui_module, tmp_path):
    plugin, settings = make_plugin()
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._base_dir_edit.setText(str(tmp_path))
    dlg._on_base_dir_changed()
    assert settings["file_io_base_dir"] == str(tmp_path.resolve())
    assert dlg._base_dir_edit.text() == str(tmp_path.resolve())


def test_base_dir_changed_nonexistent_dir_rejected(ui_module, tmp_path):
    missing = tmp_path / "nope"
    plugin, settings = make_plugin(settings={"file_io_base_dir": "/prev/good"})
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._base_dir_edit.setText(str(missing))
    dlg._on_base_dir_changed()
    assert "file_io_base_dir" not in settings or settings["file_io_base_dir"] == "/prev/good"
    plugin.context.show_status_message.assert_any_call(
        f"'{missing}' is not an existing directory — "
        "File I/O base directory was not changed.",
        5000,
    )
    assert dlg._base_dir_edit.text() == "/prev/good"


def test_base_dir_changed_file_not_dir_rejected(ui_module, tmp_path):
    a_file = tmp_path / "afile.txt"
    a_file.write_text("x", encoding="utf-8")
    plugin, settings = make_plugin()
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._base_dir_edit.setText(str(a_file))
    dlg._on_base_dir_changed()
    assert "file_io_base_dir" not in settings
    plugin.context.show_status_message.assert_called_once()


# ---------------------------------------------------------------------------
# _browse_base_dir
# ---------------------------------------------------------------------------


def test_browse_base_dir_sets_text_on_selection(ui_module, tmp_path):
    plugin, settings = make_plugin()
    dlg = ui_module.MCPStatusDialog(plugin)
    QFileDialog._next_directory = str(tmp_path)
    try:
        dlg._browse_base_dir()
    finally:
        QFileDialog._next_directory = ""
    assert dlg._base_dir_edit.text() == str(tmp_path)
    assert settings["file_io_base_dir"] == str(tmp_path)


def test_browse_base_dir_cancel_leaves_unchanged(ui_module):
    plugin, settings = make_plugin()
    dlg = ui_module.MCPStatusDialog(plugin)
    dlg._base_dir_edit.setText("/unchanged")
    QFileDialog._next_directory = ""
    dlg._browse_base_dir()
    assert dlg._base_dir_edit.text() == "/unchanged"
    assert "file_io_base_dir" not in settings

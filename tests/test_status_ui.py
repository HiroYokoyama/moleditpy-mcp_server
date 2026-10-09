"""Exercise the Status monitor with real Qt and isolated folder permissions."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402


@pytest.fixture(scope="module")
def ui():
    spec = importlib.util.spec_from_file_location(
        "status_ui_real", Path(__file__).parents[1] / "mcp_server" / "ui.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dialog(ui):
    app = QApplication.instance() or QApplication([])
    settings = {"port": 7891}
    context = SimpleNamespace(
        get_main_window=lambda: None,
        get_setting=lambda key, default=None: settings.get(key, default),
        set_setting=lambda key, value: settings.__setitem__(key, value),
        show_status_message=lambda *args: None,
    )
    plugin = SimpleNamespace(
        context=context,
        is_running=False,
        external_port=0,
        auth_token="a" * 64,
        url="http://127.0.0.1:7891/mcp",
        last_error="",
    )
    window = ui.MCPStatusDialog(plugin)
    yield window, settings, plugin
    window.close()
    app.processEvents()


def test_add_remove_copy_and_missing_folder(dialog, tmp_path):
    window, settings, _ = dialog
    folder = tmp_path / "folder with spaces"
    folder.mkdir()
    window._new_read_root_edit.setText(f'"{folder}"')
    window._add_typed_read_root()
    assert settings["file_io_read_roots"] == [str(folder.resolve())]
    assert window._read_roots_table.rowCount() == 1
    window._read_roots_table.selectRow(0)
    assert window._new_read_root_edit.text() == str(folder.resolve())
    window._copy_read_roots()
    assert QApplication.clipboard().text() == str(folder.resolve())
    folder.rmdir()
    window._poll_status()
    assert window._read_roots_table.item(0, 1).text() == "Missing"
    window._remove_read_roots()
    assert settings["file_io_read_roots"] == []


def test_invalid_and_duplicate_paths_do_not_grant_access(dialog, tmp_path):
    window, settings, _ = dialog
    assert not window._save_read_root(str(tmp_path / "missing"))
    assert "file_io_read_roots" not in settings
    window._save_read_root(str(tmp_path))
    window._save_read_root(str(tmp_path / "."))
    assert settings["file_io_read_roots"] == [str(tmp_path.resolve())]


def test_remote_approval_is_visible_and_selection_survives_refresh(dialog, tmp_path):
    window, settings, _ = dialog
    window._save_read_root(str(tmp_path))
    window._read_roots_table.selectRow(0)
    other = tmp_path / "other"
    other.mkdir()
    settings["file_io_read_roots"].append(str(other))
    window._poll_status()
    assert window._read_roots_table.rowCount() == 2
    assert window._selected_read_roots() == [str(tmp_path.resolve())]


def test_clear_all_requires_confirmation(dialog, tmp_path, ui):
    window, settings, _ = dialog
    window._save_read_root(str(tmp_path))
    with patch.object(
        ui.QMessageBox, "question", return_value=QMessageBox.StandardButton.No
    ):
        window._clear_read_roots()
    assert len(settings["file_io_read_roots"]) == 1
    with patch.object(
        ui.QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
    ):
        window._clear_read_roots()
    assert settings["file_io_read_roots"] == []


def test_url_and_configuration_match_pending_and_running_ports(dialog):
    window, _, plugin = dialog
    window._port_spin.setValue(9001)
    window._copy_url()
    assert QApplication.clipboard().text() == "http://127.0.0.1:9001/mcp"
    assert "9001" in window._config_view.toPlainText()
    plugin.is_running = True
    plugin.url = "http://127.0.0.1:8123/mcp"
    window.refresh()
    assert "8123" in window._config_view.toPlainText()
    assert not window._port_spin.isEnabled()


def test_start_failure_stays_visible_and_close_stops_refresh(dialog):
    window, _, plugin = dialog
    plugin.start = lambda **kwargs: False
    plugin.last_error = "Port already in use"
    window._toggle()
    assert "Port already in use" in window._feedback_lbl.text()
    window.close()
    assert not window._timer.isActive()


def test_enter_adds_folder_without_starting_server(dialog, tmp_path):
    from unittest.mock import Mock

    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    window, settings, plugin = dialog
    plugin.start = Mock(return_value=True)
    window.show()
    window._tabs.setCurrentIndex(1)
    window._new_read_root_edit.setFocus()
    window._new_read_root_edit.setText(str(tmp_path))
    QTest.keyClick(window._new_read_root_edit, Qt.Key.Key_Return)
    assert settings["file_io_read_roots"] == [str(tmp_path.resolve())]
    plugin.start.assert_not_called()


def test_invalid_base_folder_remains_visible_and_keeps_permissions(dialog, tmp_path):
    window, settings, _ = dialog
    settings["file_io_base_dir"] = str(tmp_path)
    window._base_dir_edit.setText(str(tmp_path / "missing"))
    window._on_base_dir_changed()
    assert settings["file_io_base_dir"] == str(tmp_path)
    assert window._base_dir_edit.text() == str(tmp_path)
    assert "not changed" in window._feedback_lbl.text()


def test_background_approval_does_not_erase_typed_path(dialog, tmp_path):
    window, settings, _ = dialog
    window._new_read_root_edit.setText("a folder I am still typing")
    settings["file_io_read_roots"] = [str(tmp_path)]
    window._poll_status()
    assert window._new_read_root_edit.text() == "a folder I am still typing"


def test_single_add_button_browses_when_empty(dialog, tmp_path, ui):
    window, settings, _ = dialog
    with patch.object(
        ui.QFileDialog, "getExistingDirectory", return_value=str(tmp_path)
    ) as browse:
        window._add_folder()
    browse.assert_called_once()
    assert settings["file_io_read_roots"] == [str(tmp_path.resolve())]

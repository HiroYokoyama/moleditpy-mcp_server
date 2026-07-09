"""
Tests for mcp_server/ui.py — MCPStatusDialog.

ui.py subclasses PyQt6.QtWidgets.QDialog, which under the headless mock
environment (see conftest.mock_optional_imports) becomes a MagicMock
instance rather than a real class, so MCPStatusDialog can't be
instantiated or even imported normally in tests (subclassing a non-type
raises TypeError). Instead we extract individual method bodies via
ast.get_source_segment + exec and run them against a lightweight fake
``self`` — this exercises the real logic without needing a QApplication.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path as _Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = _Path(__file__).resolve().parents[1]
UI_SOURCE = (ROOT / "mcp_server" / "ui.py").read_text(encoding="utf-8")


def _extract_method_as_fn(class_name: str, method_name: str):
    """Parse ui.py, pull out one method's source, and exec it into a bare function."""
    tree = ast.parse(UI_SOURCE)
    cls_node = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    method_node = next(
        n for n in cls_node.body
        if isinstance(n, ast.FunctionDef) and n.name == method_name
    )
    segment = ast.get_source_segment(UI_SOURCE, method_node)
    assert segment is not None
    namespace: dict = {"Path": __import__("pathlib").Path}
    exec(textwrap.dedent(segment), namespace)  # noqa: S102
    return namespace[method_name]


@pytest.fixture()
def on_base_dir_changed():
    return _extract_method_as_fn("MCPStatusDialog", "_on_base_dir_changed")


def _make_fake_self(text: str, saved: str | None = None):
    """A duck-typed stand-in for MCPStatusDialog with just what the method touches."""
    fake = SimpleNamespace()
    fake._base_dir_edit = MagicMock()
    fake._base_dir_edit.text.return_value = text
    fake._plugin = MagicMock()
    fake._plugin.context.get_setting.return_value = saved
    return fake


# ---------------------------------------------------------------------------
# _on_base_dir_changed
# ---------------------------------------------------------------------------


def test_empty_text_clears_setting(on_base_dir_changed):
    fake = _make_fake_self("")
    on_base_dir_changed(fake)
    fake._plugin.context.set_setting.assert_called_once_with("file_io_base_dir", None)


def test_whitespace_only_text_clears_setting(on_base_dir_changed):
    fake = _make_fake_self("   ")
    on_base_dir_changed(fake)
    fake._plugin.context.set_setting.assert_called_once_with("file_io_base_dir", None)


def test_existing_directory_is_accepted_and_resolved(on_base_dir_changed, tmp_path):
    fake = _make_fake_self(str(tmp_path))
    on_base_dir_changed(fake)
    fake._plugin.context.set_setting.assert_called_once()
    key, value = fake._plugin.context.set_setting.call_args.args
    assert key == "file_io_base_dir"
    assert _Path(value) == tmp_path.resolve()
    fake._base_dir_edit.setText.assert_called_once_with(value)


def test_nonexistent_directory_is_rejected(on_base_dir_changed, tmp_path):
    missing = tmp_path / "does_not_exist"
    fake = _make_fake_self(str(missing), saved="/previous/valid/dir")
    on_base_dir_changed(fake)
    # Regression: previously any typed text (even a typo) was persisted as
    # the sandbox base_dir with no validation, unlike the set_file_io_config
    # MCP tool which requires an existing directory. A bad path here would
    # silently sandbox every file I/O tool to a directory that can never
    # resolve, failing confusingly on the next write/read/list call.
    fake._plugin.context.set_setting.assert_not_called()
    fake._plugin.context.show_status_message.assert_called_once()
    message = fake._plugin.context.show_status_message.call_args.args[0]
    assert "not an existing directory" in message
    # The field is reverted to the last known-good value, not left showing
    # the rejected input.
    fake._base_dir_edit.setText.assert_called_once_with("/previous/valid/dir")


def test_nonexistent_directory_with_no_prior_value_reverts_to_empty(
    on_base_dir_changed, tmp_path
):
    missing = tmp_path / "nope"
    fake = _make_fake_self(str(missing), saved=None)
    on_base_dir_changed(fake)
    fake._plugin.context.set_setting.assert_not_called()
    fake._base_dir_edit.setText.assert_called_once_with("")


def test_file_path_is_rejected_not_a_directory(on_base_dir_changed, tmp_path):
    a_file = tmp_path / "notadir.txt"
    a_file.write_text("x", encoding="utf-8")
    fake = _make_fake_self(str(a_file))
    on_base_dir_changed(fake)
    fake._plugin.context.set_setting.assert_not_called()
    fake._plugin.context.show_status_message.assert_called_once()

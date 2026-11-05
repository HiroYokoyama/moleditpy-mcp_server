"""Plugin-local settings survive reloads without writing the host's settings."""

import json
import os
from unittest.mock import Mock

import pytest

from mcp_server.settings import SettingsContext


def legacy_context(values):
    context = Mock()
    context.get_setting.side_effect = lambda key, default=None: values.get(key, default)
    return context


def test_migration_preserves_token_and_file_permissions_without_deleting_old_values(
    tmp_path,
):
    values = {
        "auth_token": "a" * 64,
        "auto_start": True,
        "port": 7892,
        "protocol_mode": "legacy",
        "file_io_base_dir": "/data/myproject",
        "file_io_allowed_extensions": [".xyz"],
        "file_io_read_roots": ["/data/shared"],
    }
    original = dict(values)
    host = legacy_context(values)
    path = tmp_path / "settings.json"
    settings = SettingsContext(host, path)
    assert json.loads(path.read_text(encoding="utf-8")) == values
    settings.set_setting("auto_start", False)
    assert values == original
    host.set_setting.assert_not_called()
    assert settings.get_setting("auth_token") == original["auth_token"]
    settings.show_status_message("saved")
    host.show_status_message.assert_called_once_with("saved")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_existing_local_file_wins_and_missing_keys_use_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"port": 9999}', encoding="utf-8")
    host = legacy_context({"port": 7891, "auto_start": True})
    settings = SettingsContext(host, path)
    assert settings.get_setting("port") == 9999
    assert settings.get_setting("auto_start", False) is False
    host.get_setting.assert_not_called()


def test_reload_and_separate_context_writes_preserve_other_keys(tmp_path):
    path = tmp_path / "settings.json"
    first = SettingsContext(legacy_context({}), path)
    second = SettingsContext(legacy_context({}), path)
    first.set_setting("port", 9999)
    second.set_setting("file_io_base_dir", "example")
    restored = SettingsContext(legacy_context({"port": 7891}), path)
    assert restored.get_setting("port") == 9999
    assert restored.get_setting("file_io_base_dir") == "example"


@pytest.mark.parametrize("content", ["{broken", "[]", "null"])
def test_invalid_local_settings_are_not_overwritten_or_remigrated(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")
    host = legacy_context({"auth_token": "a" * 64})
    with pytest.raises((ValueError, TypeError)):
        SettingsContext(host, path)
    assert path.read_text(encoding="utf-8") == content
    host.get_setting.assert_not_called()


def test_failed_atomic_save_keeps_previous_file_and_removes_temporary(
    tmp_path, monkeypatch
):
    path = tmp_path / "settings.json"
    settings = SettingsContext(legacy_context({"port": 7891}), path)
    original = path.read_bytes()
    monkeypatch.setattr(
        "mcp_server.settings.os.replace", Mock(side_effect=OSError("disk error"))
    )
    with pytest.raises(OSError, match="disk error"):
        settings.set_setting("port", 9999)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_bad_legacy_types_are_ignored_and_explicit_null_is_kept(tmp_path):
    settings = SettingsContext(
        legacy_context({"port": True, "auto_start": "yes", "file_io_base_dir": None}),
        tmp_path / "settings.json",
    )
    assert settings.get_setting("port", 7891) == 7891
    assert settings.get_setting("auto_start", False) is False
    assert settings.get_setting("file_io_base_dir", "default") is None

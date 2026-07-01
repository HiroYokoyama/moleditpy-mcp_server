"""
Integration tests against the real PluginContext.

Skipped automatically when moleditpy is not installed.
Install with: pip install moleditpy
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pi = pytest.importorskip(
    "moleditpy.plugins.plugin_interface",
    reason="moleditpy not installed",
)
PluginContext = pi.PluginContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(plugin_name: str = "mcp_server") -> PluginContext:
    """Real PluginContext backed by a minimal mock manager and MainWindow."""
    mw = MagicMock()
    mw.init_manager.settings = {}
    mw.init_manager.settings_dirty = False
    manager = MagicMock()
    manager.get_main_window.return_value = mw
    return PluginContext(manager, plugin_name)


def _settings(ctx: PluginContext) -> dict:
    return ctx._manager.get_main_window().init_manager.settings


# ---------------------------------------------------------------------------
# Setting namespacing
# ---------------------------------------------------------------------------


class TestSettingNamespacing:
    """
    Verify that the plugin's get_setting / set_setting calls go through the
    real PluginContext namespacing (plugin.<name>.<key>), not a mock.
    This is the main gap that unit tests using MagicMock ctx cannot cover.
    """

    def test_set_stores_namespaced_key(self):
        ctx = _make_context()
        ctx.set_setting("auto_start", True)
        assert _settings(ctx)["plugin.mcp_server.auto_start"] is True

    def test_get_reads_namespaced_key(self):
        ctx = _make_context()
        _settings(ctx)["plugin.mcp_server.auto_start"] = True
        assert ctx.get_setting("auto_start", False) is True

    def test_get_returns_default_when_missing(self):
        assert _make_context().get_setting("auto_start", False) is False

    def test_file_io_base_dir_namespaced(self):
        ctx = _make_context()
        ctx.set_setting("file_io_base_dir", "/home/user/calc")
        assert _settings(ctx)["plugin.mcp_server.file_io_base_dir"] == "/home/user/calc"

    def test_file_io_allowed_extensions_namespaced(self):
        ctx = _make_context()
        ctx.set_setting("file_io_allowed_extensions", [".xyz", ".inp"])
        assert _settings(ctx)["plugin.mcp_server.file_io_allowed_extensions"] == [
            ".xyz",
            ".inp",
        ]

    def test_namespaces_are_isolated(self):
        ctx_a = _make_context("mcp_server")
        ctx_b = _make_context("other_plugin")
        ctx_a.set_setting("auto_start", True)
        ctx_b.set_setting("auto_start", False)
        assert _settings(ctx_a)["plugin.mcp_server.auto_start"] is True
        assert _settings(ctx_b)["plugin.other_plugin.auto_start"] is False


# ---------------------------------------------------------------------------
# initialize()
# ---------------------------------------------------------------------------


class TestInitialize:
    def test_registers_three_menu_items(self):
        import mcp_server as pkg

        ctx = _make_context()
        pkg.initialize(ctx)
        paths = [c.args[1] for c in ctx._manager.register_menu_action.call_args_list]
        assert any("Status" in p for p in paths)
        assert any("Start" in p for p in paths)
        assert any("Stop" in p for p in paths)

    def test_menu_items_nested_under_plugin_menu(self):
        import mcp_server as pkg

        ctx = _make_context()
        pkg.initialize(ctx)
        paths = [c.args[1] for c in ctx._manager.register_menu_action.call_args_list]
        assert all(p.startswith("Plugin/") for p in paths)

    def test_no_auto_start_by_default(self):
        import mcp_server as pkg

        ctx = _make_context()
        pkg.initialize(ctx)
        assert not pkg._plugin.is_running

    def test_no_auto_start_when_setting_false(self):
        import mcp_server as pkg

        ctx = _make_context()
        _settings(ctx)["plugin.mcp_server.auto_start"] = False
        pkg.initialize(ctx)
        assert not pkg._plugin.is_running

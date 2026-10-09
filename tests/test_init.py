"""Tests for mcp_server/__init__.py — plugin entry point."""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from conftest import make_context, mock_optional_imports

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_mcp_submodules() -> dict:
    """Remove mcp_server.* submodules from sys.modules; return snapshot."""
    saved = {
        k: v
        for k, v in sys.modules.items()
        if k == "mcp_server" or k.startswith("mcp_server.")
    }
    for k in saved:
        del sys.modules[k]
    return saved


def _restore_mcp_submodules(saved: dict) -> None:
    for k in list(sys.modules):
        if k == "mcp_server" or k.startswith("mcp_server."):
            del sys.modules[k]
    sys.modules.update(saved)


@contextmanager
def _mock_server_modules():
    """
    Replace mcp_server.bridge and mcp_server.server in sys.modules with
    lightweight fakes so MCPServerPlugin.start() works without Qt or a
    real HTTP server.
    """
    mock_bridge = MagicMock()
    mock_bridge_cls = MagicMock(return_value=mock_bridge)

    _is_running = [False]

    class _FakeServer:
        def __init__(self, *args, **kwargs):
            self._port = kwargs.get("port", 7891)

        def start(self):
            _is_running[0] = True

        def stop(self):
            _is_running[0] = False

        @property
        def is_running(self):
            return _is_running[0]

        @property
        def url(self):
            return f"http://127.0.0.1:{self._port}/mcp"

    fake_bridge_mod = types.ModuleType("mcp_server.bridge")
    fake_bridge_mod.MCPBridge = mock_bridge_cls

    fake_server_mod = types.ModuleType("mcp_server.server")
    fake_server_mod.MCPHttpServer = _FakeServer
    # Nothing else is listening: the default for every lifecycle test.
    fake_server_mod.is_port_serving = MagicMock(return_value=False)

    with mock_optional_imports():
        saved = {
            "mcp_server.bridge": sys.modules.get("mcp_server.bridge"),
            "mcp_server.server": sys.modules.get("mcp_server.server"),
        }
        sys.modules["mcp_server.bridge"] = fake_bridge_mod
        sys.modules["mcp_server.server"] = fake_server_mod
        try:
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pkg():
    """Import mcp_server with a clean sys.modules slate; restore on teardown."""
    saved = _clear_mcp_submodules()
    with mock_optional_imports():
        import mcp_server as _pkg

        yield _pkg
    _restore_mcp_submodules(saved)


@pytest.fixture()
def ctx():
    return make_context()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_metadata_constants(pkg):
    assert pkg.PLUGIN_NAME
    assert pkg.PLUGIN_VERSION
    assert pkg.PLUGIN_AUTHOR
    assert pkg.PLUGIN_DESCRIPTION
    assert pkg.PLUGIN_SUPPORTED_MOLEDITPY_VERSION.startswith(">=4")


def test_version_is_semver(pkg):
    parts = pkg.PLUGIN_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_authentication_token_is_random_and_persisted(pkg, ctx):
    settings = {}
    ctx.get_setting.side_effect = lambda key, default=None: settings.get(key, default)
    ctx.set_setting.side_effect = lambda key, value: settings.__setitem__(key, value)
    first = pkg.MCPServerPlugin(ctx)
    second = pkg.MCPServerPlugin(ctx)
    assert len(first.auth_token) == 64
    assert first.auth_token == second.auth_token == settings["auth_token"]


# ---------------------------------------------------------------------------
# initialize()
# ---------------------------------------------------------------------------


def test_initialize_registers_plugin_menus(pkg, ctx):
    pkg.initialize(ctx)
    assert ctx.add_plugin_menu.call_count >= 3


def test_initialize_menu_paths_contain_mcp_server(pkg, ctx):
    pkg.initialize(ctx)
    calls = ctx.add_plugin_menu.call_args_list
    paths = [c.args[0] for c in calls]
    assert all("MCP Server" in p for p in paths)


def test_initialize_sets_plugin_singleton(pkg, ctx):
    pkg._plugin = None
    pkg.initialize(ctx)
    assert pkg._plugin is not None


def test_initialize_no_auto_start_by_default(pkg, ctx):
    ctx.get_setting.return_value = False
    pkg.initialize(ctx)
    assert not pkg._plugin.is_running


def test_initialize_auto_starts_when_setting_true(pkg, ctx):
    ctx.get_setting.side_effect = lambda key, default=None: {
        "auto_start": True,
        "port": 0,
    }.get(key, default)
    with _mock_server_modules():
        pkg.initialize(ctx)
        assert pkg._plugin.is_running
        pkg._plugin.stop()


def test_initialize_idempotent_plugin_instance(pkg, ctx):
    pkg.initialize(ctx)
    first = pkg._plugin
    pkg.initialize(ctx)
    assert pkg._plugin is not None
    # A second initialize always creates a new singleton
    assert pkg._plugin is not first


# ---------------------------------------------------------------------------
# MCPServerPlugin lifecycle
# ---------------------------------------------------------------------------


def test_start_stop_lifecycle(pkg, ctx):
    ctx.get_setting.side_effect = lambda key, default=None: {
        "port": 0,
        "auto_start": False,
    }.get(key, default)
    with _mock_server_modules():
        plugin = pkg.MCPServerPlugin(ctx)
        assert not plugin.is_running
        plugin.start(port=0)
        assert plugin.is_running
        plugin.stop()
        assert not plugin.is_running


def test_start_returns_false_if_already_running(pkg, ctx):
    ctx.get_setting.return_value = 0
    with _mock_server_modules():
        plugin = pkg.MCPServerPlugin(ctx)
        plugin.start(port=0)
        result = plugin.start(port=0)
        assert result is False
        plugin.stop()


def test_url_reflects_configured_port(pkg, ctx):
    ctx.get_setting.side_effect = lambda key, default=None: {
        "port": 9999,
        "auto_start": False,
    }.get(key, default)
    plugin = pkg.MCPServerPlugin(ctx)
    assert "9999" in plugin.url


def test_stop_when_not_running_is_safe(pkg, ctx):
    plugin = pkg.MCPServerPlugin(ctx)
    plugin.stop()  # should not raise


def test_start_reports_failure_on_unexpected_exception(pkg, ctx):
    """start() must never propagate an unexpected (non-OSError) exception.

    Regression test: start() previously only caught OSError, so any other
    failure (e.g. a broken PluginContext, an import error surfaced as
    RuntimeError/AttributeError) would crash the caller — the "Start
    Server" menu action — instead of being reported via show_status_message
    like every other startup failure.
    """
    ctx.get_setting.return_value = 0
    with _mock_server_modules():
        sys.modules["mcp_server.bridge"].MCPBridge = MagicMock(
            side_effect=RuntimeError("boom")
        )
        plugin = pkg.MCPServerPlugin(ctx)
        result = plugin.start(port=0)
        assert result is False
        assert plugin._bridge is None
        assert plugin._server is None
        assert not plugin.is_running
        ctx.show_status_message.assert_called_once()
        message = ctx.show_status_message.call_args[0][0]
        assert "boom" in message


def test_show_status_opens_dialog(pkg, ctx):
    ctx.get_window.return_value = None
    with mock_optional_imports():
        sys.modules["mcp_server.ui"] = MagicMock()
        try:
            plugin = pkg.MCPServerPlugin(ctx)
            plugin.show_status()
        finally:
            sys.modules.pop("mcp_server.ui", None)
    ctx.register_window.assert_called_once()


def test_show_status_reuses_existing_visible_window(pkg, ctx):
    """If a status dialog is already open, raise/activate it instead of
    creating a second one."""
    win = MagicMock()
    win.isVisible.return_value = True
    ctx.get_window.return_value = win
    plugin = pkg.MCPServerPlugin(ctx)
    plugin.show_status()
    win.raise_.assert_called_once()
    win.activateWindow.assert_called_once()
    ctx.register_window.assert_not_called()


def test_url_uses_running_server_url(pkg, ctx):
    """Once the server is running, url reflects the actual bound port
    (the server object's url), not just the configured setting."""
    ctx.get_setting.return_value = 7891
    with _mock_server_modules():
        plugin = pkg.MCPServerPlugin(ctx)
        plugin.start(port=12345)
        assert plugin.url == "http://127.0.0.1:12345/mcp"
        plugin.stop()


# ---------------------------------------------------------------------------
# Protocol mode wiring
# ---------------------------------------------------------------------------


def _capture_server_kwargs():
    """Fake server module that records the kwargs MCPServerPlugin passes."""
    recorded = {}

    class _RecordingServer:
        def __init__(self, *args, **kwargs):
            recorded.update(kwargs)

        def start(self):
            pass

        def stop(self):
            pass

        @property
        def is_running(self):
            return True

        @property
        def url(self):
            return "http://127.0.0.1:0/mcp"

    return recorded, _RecordingServer


def test_start_passes_saved_protocol_mode(pkg, ctx):
    ctx.get_setting.side_effect = lambda key, default=None: {
        "port": 0,
        "protocol_mode": "modern",
    }.get(key, default)
    recorded, server_cls = _capture_server_kwargs()
    with _mock_server_modules():
        sys.modules["mcp_server.server"].MCPHttpServer = server_cls
        pkg.MCPServerPlugin(ctx).start(port=0)
    assert recorded["protocol_mode"] == "modern"
    assert recorded["server_version"] == pkg.PLUGIN_VERSION


def test_start_defaults_protocol_mode_to_auto(pkg, ctx):
    ctx.get_setting.side_effect = lambda key, default=None: default
    recorded, server_cls = _capture_server_kwargs()
    with _mock_server_modules():
        sys.modules["mcp_server.server"].MCPHttpServer = server_cls
        pkg.MCPServerPlugin(ctx).start(port=0)
    assert recorded["protocol_mode"] == "auto"


# ---------------------------------------------------------------------------
# Another instance already serves the port
# ---------------------------------------------------------------------------


def test_start_skips_when_another_instance_serves_port(pkg, ctx):
    ctx.get_setting.return_value = 7891
    with _mock_server_modules():
        sys.modules["mcp_server.server"].is_port_serving.return_value = True
        plugin = pkg.MCPServerPlugin(ctx)
        assert plugin.start(port=7891) is False
        assert plugin._server is None
        assert plugin._bridge is None
        message = ctx.show_status_message.call_args[0][0]
        assert "another MoleditPy instance" in message
        assert "failed" not in message


def test_start_probes_the_requested_port(pkg, ctx):
    ctx.get_setting.return_value = 0
    with _mock_server_modules():
        probe = sys.modules["mcp_server.server"].is_port_serving
        pkg.MCPServerPlugin(ctx).start(port=1234)
        probe.assert_called_once_with("127.0.0.1", 1234)


def test_auto_start_defers_to_another_instance(pkg, ctx):
    ctx.get_setting.side_effect = lambda key, default=None: {
        "auto_start": True,
        "port": 7891,
    }.get(key, default)
    with _mock_server_modules():
        sys.modules["mcp_server.server"].is_port_serving.return_value = True
        pkg.initialize(ctx)
        assert not pkg._plugin.is_running


def test_external_port_reports_other_instance(pkg, ctx):
    ctx.get_setting.side_effect = lambda key, default=None: {"port": 7891}.get(
        key, default
    )
    with _mock_server_modules():
        probe = sys.modules["mcp_server.server"].is_port_serving
        plugin = pkg.MCPServerPlugin(ctx)
        probe.return_value = True
        assert plugin.external_port == 7891
        probe.return_value = False  # the other instance exited: not cached
        assert plugin.external_port == 0


def test_external_port_is_zero_when_this_instance_runs(pkg, ctx):
    ctx.get_setting.return_value = 0
    with _mock_server_modules():
        plugin = pkg.MCPServerPlugin(ctx)
        plugin.start(port=0)
        sys.modules["mcp_server.server"].is_port_serving.return_value = True
        assert plugin.external_port == 0
        plugin.stop()


# ---------------------------------------------------------------------------
# Reload: the host re-executes the module and calls no teardown hook
# ---------------------------------------------------------------------------


def _registry_backed(ctx):
    """Make ctx keep registered objects the way the host's per-plugin registry does."""
    registry = {}
    ctx.register_window.side_effect = registry.__setitem__
    ctx.get_window.side_effect = registry.get
    return registry


def _settings(ctx, **values):
    ctx.get_setting.side_effect = lambda key, default=None: values.get(key, default)


def test_reload_stops_the_previous_server_and_starts_the_new_one(pkg, ctx):
    _registry_backed(ctx)
    _settings(ctx, auto_start=True, port=0)
    with _mock_server_modules():
        pkg.initialize(ctx)
        first = pkg._plugin
        assert first.is_running
        pkg.initialize(ctx)
        assert first._server is None  # stopped and released
        assert pkg._plugin is not first
        assert pkg._plugin.is_running
        pkg._plugin.stop()


def test_reload_restores_a_manually_started_server(pkg, ctx):
    _registry_backed(ctx)
    _settings(ctx, auto_start=False, port=0)
    with _mock_server_modules():
        pkg.initialize(ctx)
        assert not pkg._plugin.is_running
        pkg._plugin.start(port=0)
        pkg.initialize(ctx)
        assert pkg._plugin.is_running
        pkg._plugin.stop()


def test_reload_leaves_a_stopped_server_stopped(pkg, ctx):
    _registry_backed(ctx)
    _settings(ctx, auto_start=False, port=0)
    with _mock_server_modules():
        pkg.initialize(ctx)
        pkg.initialize(ctx)
        assert not pkg._plugin.is_running


def test_reload_never_reports_its_own_old_server_as_another_instance(pkg, ctx):
    _registry_backed(ctx)
    _settings(ctx, auto_start=True, port=0)
    with _mock_server_modules():
        pkg.initialize(ctx)
        ctx.show_status_message.reset_mock()
        pkg.initialize(ctx)
        messages = [c[0][0] for c in ctx.show_status_message.call_args_list]
        assert not any("another MoleditPy instance" in m for m in messages)
        pkg._plugin.stop()


def test_reload_closes_the_previous_status_dialog(pkg, ctx):
    registry = _registry_backed(ctx)
    _settings(ctx, auto_start=False)
    dialog = MagicMock()
    registry["status_dialog"] = dialog
    pkg.initialize(ctx)
    dialog.close.assert_called_once_with()


def test_reload_survives_a_previous_plugin_that_cannot_stop(pkg, ctx):
    registry = _registry_backed(ctx)
    _settings(ctx, auto_start=False)
    broken = MagicMock()
    broken.stop.side_effect = RuntimeError("boom")
    registry["server_plugin"] = broken
    pkg.initialize(ctx)  # must not raise
    assert registry["server_plugin"] is pkg._plugin

"""
Tests for mcp_server/bridge.py's MCPBridge class that genuinely import the
module.

Unlike test_bridge.py (which loads bridge.py's module-level execute_operation
dispatch logic under the blanket MagicMock PyQt6 mock), this file installs
real, subclassable PyQt6.QtCore stand-ins from bridge_qt_stubs.py *before*
importing mcp_server.bridge, so MCPBridge — a QObject subclass — is actually
importable/instantiable and its __init__/call/_on_request statements are
executed and counted toward coverage (mirrors test_ui_dialog.py's approach
for MCPStatusDialog).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from bridge_qt_stubs import install_bridge_qt_stubs, remove_bridge_qt_stubs


@pytest.fixture()
def bridge_module():
    """Install rich Qt stubs, freshly import mcp_server.bridge, then clean up."""
    saved = {
        k: v
        for k, v in sys.modules.items()
        if k.startswith("PyQt6") or k == "mcp_server.bridge"
    }
    install_bridge_qt_stubs()
    try:
        import mcp_server.bridge as mod  # noqa: PLC0415 - intentional fresh import

        yield mod
    finally:
        remove_bridge_qt_stubs()
        for k in list(sys.modules):
            if k.startswith("PyQt6") or k == "mcp_server.bridge":
                del sys.modules[k]
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


def test_mcpbridge_init_stores_context_and_connects_signal(bridge_module):
    ctx = MagicMock()
    bridge = bridge_module.MCPBridge(ctx)
    assert bridge._context is ctx
    assert bridge._request._fns == [bridge._on_request]


def test_mcpbridge_init_accepts_parent(bridge_module):
    ctx = MagicMock()
    parent = MagicMock()
    bridge = bridge_module.MCPBridge(ctx, parent)
    assert bridge._parent is parent


# ---------------------------------------------------------------------------
# call() / _on_request()
# ---------------------------------------------------------------------------


def test_mcpbridge_call_returns_result(bridge_module, monkeypatch):
    ctx = MagicMock()
    bridge = bridge_module.MCPBridge(ctx)
    monkeypatch.setattr(
        bridge_module, "execute_operation", lambda c, op, a: {"echo": op, "args": a}
    )
    result = bridge.call("get_molecule_info", {"x": 1})
    assert result == {"echo": "get_molecule_info", "args": {"x": 1}}


def test_mcpbridge_call_defaults_args_to_empty_dict(bridge_module, monkeypatch):
    ctx = MagicMock()
    bridge = bridge_module.MCPBridge(ctx)
    captured = {}

    def _fake(c, op, a):
        captured["args"] = a
        return None

    monkeypatch.setattr(bridge_module, "execute_operation", _fake)
    bridge.call("refresh_ui")
    assert captured["args"] == {}


def test_mcpbridge_call_error_propagates(bridge_module, monkeypatch):
    ctx = MagicMock()
    bridge = bridge_module.MCPBridge(ctx)

    def _raise(c, op, a):
        raise ValueError("boom")

    monkeypatch.setattr(bridge_module, "execute_operation", _raise)
    with pytest.raises(ValueError, match="boom"):
        bridge.call("bad_op")


def test_mcpbridge_call_timeout_raises(bridge_module):
    ctx = MagicMock()
    bridge = bridge_module.MCPBridge(ctx)
    # Detach the connected slot so the event is never set -> wait() times out.
    bridge._request._fns.clear()
    with pytest.raises(TimeoutError, match="timed out"):
        bridge.call("noop", timeout=0.05)

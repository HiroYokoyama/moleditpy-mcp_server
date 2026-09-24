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
import threading
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


def _call_off_thread(bridge, *args, **kwargs):
    """Run bridge.call() from a worker thread, as the HTTP server does."""
    outcome = {}

    def _run():
        try:
            outcome["result"] = bridge.call(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised on the test thread
            outcome["error"] = exc

    worker = threading.Thread(target=_run)
    worker.start()
    worker.join(5)
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def test_mcpbridge_call_returns_result(bridge_module, monkeypatch):
    ctx = MagicMock()
    bridge = bridge_module.MCPBridge(ctx)
    monkeypatch.setattr(
        bridge_module, "execute_operation", lambda c, op, a: {"echo": op, "args": a}
    )
    result = _call_off_thread(bridge, "get_molecule_info", {"x": 1})
    assert result == {"echo": "get_molecule_info", "args": {"x": 1}}


def test_mcpbridge_call_defaults_args_to_empty_dict(bridge_module, monkeypatch):
    ctx = MagicMock()
    bridge = bridge_module.MCPBridge(ctx)
    captured = {}

    def _fake(c, op, a):
        captured["args"] = a
        return None

    monkeypatch.setattr(bridge_module, "execute_operation", _fake)
    _call_off_thread(bridge, "refresh_ui")
    assert captured["args"] == {}


def test_mcpbridge_call_error_propagates(bridge_module, monkeypatch):
    ctx = MagicMock()
    bridge = bridge_module.MCPBridge(ctx)

    def _raise(c, op, a):
        raise ValueError("boom")

    monkeypatch.setattr(bridge_module, "execute_operation", _raise)
    with pytest.raises(ValueError, match="boom"):
        _call_off_thread(bridge, "bad_op")


def test_mcpbridge_call_timeout_raises(bridge_module):
    ctx = MagicMock()
    bridge = bridge_module.MCPBridge(ctx)
    # Detach the connected slot so the event is never set -> wait() times out.
    bridge._request._fns.clear()
    with pytest.raises(TimeoutError, match="timed out"):
        _call_off_thread(bridge, "noop", timeout=0.05)


def test_mcpbridge_timed_out_request_is_not_run_later(bridge_module, monkeypatch):
    """A request the caller gave up on must not be applied when the busy
    main thread finally reaches it."""
    ctx = MagicMock()
    bridge = bridge_module.MCPBridge(ctx)
    ran = []
    monkeypatch.setattr(bridge_module, "execute_operation", lambda c, op, a: ran.append(op))
    queued = []
    bridge._request._fns[:] = [lambda *a: queued.append(a)]  # hold, do not deliver
    with pytest.raises(TimeoutError):
        _call_off_thread(bridge, "clear_canvas", timeout=0.05)
    bridge._on_request(*queued[0])  # the main thread gets to it late
    assert ran == []
    assert not queued[0][2]["event"].is_set()


def test_mcpbridge_call_on_owner_thread_runs_directly(bridge_module, monkeypatch):
    """Called from the Qt main thread itself, waiting on a queued signal
    would deadlock until the timeout; the call must run inline instead."""
    ctx = MagicMock()
    bridge = bridge_module.MCPBridge(ctx)
    bridge._request._fns.clear()  # a queued delivery would never happen
    monkeypatch.setattr(bridge_module, "execute_operation", lambda c, op, a: {"op": op})
    assert bridge.call("refresh_ui", timeout=0.05) == {"op": "refresh_ui"}

"""The MCP calls that would block on a modal dialog: the "Unsaved Changes"
override for XYZ loading (mocked app) and the bridge's general modal watch
(real Qt, in a clean interpreter)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from conftest import load_module, make_bridge, make_context, mock_optional_imports

WATER = "3\nwater\nO 0.0 0.0 0.0\nH 0.76 0.59 0.0\nH -0.76 0.59 0.0"


@pytest.fixture()
def bridge_mod():
    with mock_optional_imports():
        yield load_module("bridge.py")


@pytest.fixture()
def srv():
    with mock_optional_imports():
        yield load_module("server.py")


class FakeStateManager:
    """Stand-in for the app's StateManager: the real check would open a box."""

    def __init__(self, unsaved: bool, atoms: dict) -> None:
        self.has_unsaved_changes = unsaved
        self.data = MagicMock(atoms=atoms)

    def check_unsaved_changes(self) -> bool:
        raise AssertionError("the app's Unsaved Changes box would have opened")


def _ctx_with(sm: FakeStateManager, current_3d=None):
    ctx = make_context()
    mw = ctx.get_main_window.return_value
    mw.state_manager = sm
    mw.view_3d_manager.current_mol = current_3d
    seen = []

    def _show(xyz_text, source_name="XYZ data"):
        if not sm.check_unsaved_changes():
            return None
        seen.append(xyz_text)
        mol = MagicMock()
        mol.HasProp.return_value = False
        mol.GetNumAtoms.return_value = 3
        mol.GetNumBonds.return_value = 2
        return mol

    ctx.show_xyz_data.side_effect = _show
    return ctx, seen


# ---------------------------------------------------------------------------
# Unsaved-changes override (bridge)
# ---------------------------------------------------------------------------


def test_show_xyz_refuses_to_discard_unsaved_work(bridge_mod):
    sm = FakeStateManager(True, {1: {}})
    ctx, seen = _ctx_with(sm)
    with pytest.raises(ValueError, match="discard_unsaved=true"):
        bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": WATER})
    assert seen == []
    assert "check_unsaved_changes" not in vars(sm)


def test_show_xyz_discards_when_asked(bridge_mod):
    sm = FakeStateManager(True, {1: {}})
    ctx, seen = _ctx_with(sm)
    result = bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": WATER, "discard_unsaved": True}
    )
    assert result["success"] is True
    assert seen == [WATER]
    assert "check_unsaved_changes" not in vars(sm)


@pytest.mark.parametrize(
    "unsaved,atoms,current_3d",
    [(False, {1: {}}, object()), (True, {}, None)],
    ids=["saved", "empty-document"],
)
def test_show_xyz_loads_when_nothing_would_be_lost(
    bridge_mod, unsaved, atoms, current_3d
):
    sm = FakeStateManager(unsaved, atoms)
    ctx, seen = _ctx_with(sm, current_3d)
    result = bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": WATER})
    assert result["success"] is True
    assert seen == [WATER]


def test_unsaved_3d_only_document_counts_as_work(bridge_mod):
    sm = FakeStateManager(True, {})
    ctx, seen = _ctx_with(sm, current_3d=object())
    with pytest.raises(ValueError, match="unsaved changes"):
        bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": WATER})
    assert seen == []


def test_override_restores_an_instance_level_check(bridge_mod):
    sm = FakeStateManager(True, {1: {}})
    original = MagicMock(return_value=True)
    sm.check_unsaved_changes = original
    ctx, _ = _ctx_with(sm)
    ctx.show_xyz_data.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": WATER})
    assert sm.check_unsaved_changes is original


def test_override_without_state_manager_is_a_no_op(bridge_mod):
    ctx = make_context()
    ctx.get_main_window.return_value = None
    with bridge_mod._unsaved_changes_override(ctx, False) as state:
        pass
    assert state == {"blocked": False}


# ---------------------------------------------------------------------------
# Server: schema, instructions, pass-through
# ---------------------------------------------------------------------------


def _tool(srv, name):
    return next(t for t in srv._TOOLS if t["name"] == name)


@pytest.mark.parametrize("name", ["show_xyz_in_viewer", "load_xyz_file"])
def test_xyz_tools_take_discard_unsaved(srv, name):
    prop = _tool(srv, name)["inputSchema"]["properties"]["discard_unsaved"]
    assert prop["type"] == "boolean"
    assert "unsaved" in prop["description"]


def test_instructions_mention_discard_unsaved(srv):
    assert "discard_unsaved" in srv._SERVER_INSTRUCTIONS


def test_show_xyz_forwards_discard_unsaved(srv):
    bridge = make_bridge({"show_xyz": {"success": True}})
    srv.dispatch_tool(
        bridge, "show_xyz_in_viewer", {"xyz_text": WATER, "discard_unsaved": True}
    )
    sent = bridge.call.call_args_list[0].args[1]
    assert sent["discard_unsaved"] is True


def test_unsaved_error_reaches_the_client(srv):
    bridge = MagicMock()
    bridge.call.side_effect = ValueError("has unsaved changes ... discard_unsaved=true")
    result = srv.dispatch_tool(bridge, "show_xyz_in_viewer", {"xyz_text": WATER})
    assert result["isError"] is True
    assert "discard_unsaved=true" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Modal watch (real Qt, clean interpreter)
# ---------------------------------------------------------------------------


def _run_qt(script: str) -> None:
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    if result.returncode == 77:
        pytest.skip("PySide6 unavailable")
    assert result.returncode == 0, result.stdout + result.stderr


_QT_PRELUDE = """
import importlib.util, threading, time
try:
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QTimer
except ImportError:
    raise SystemExit(77)
spec = importlib.util.spec_from_file_location('real_bridge', {source!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
app = QApplication([])
bridge = module.MCPBridge(object())
ran = []
def op(ctx, operation, args):
    box = QMessageBox()
    box.setWindowTitle('Unsaved Changes')
    QTimer.singleShot({close_ms}, box.reject)
    box.exec()
    ran.append(operation)
    return {{'finished': operation}}
module.execute_operation = op
out = []
def call(operation):
    try:
        out.append(('ok', bridge.call(operation, timeout=10), time.monotonic()))
    except Exception as exc:
        out.append((type(exc).__name__, str(exc), time.monotonic()))
def run(operation):
    worker = threading.Thread(target=call, args=(operation,))
    worker.start()
    deadline = time.monotonic() + 10
    while worker.is_alive() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.join(1)
    return out.pop()
"""


def test_modal_dialog_returns_an_error_instead_of_timing_out():
    source = Path(__file__).resolve().parents[1] / "mcp_server" / "bridge.py"
    script = (
        _QT_PRELUDE.format(source=str(source), close_ms=3000)
        + """
start = time.monotonic()
kind, message, answered = run('show_xyz')
elapsed = answered - start
assert kind == 'RuntimeError', (kind, message)
assert 'Unsaved Changes' in message and 'waiting for the user' in message, message
assert elapsed < 2.5, elapsed  # answered by the watch, not the dialog closing
# The operation still finishes once the dialog goes; its result is dropped.
deadline = time.monotonic() + 5
while not ran and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
assert ran == ['show_xyz']
"""
    )
    _run_qt(script)


def test_approval_dialogs_are_not_cut_short():
    source = Path(__file__).resolve().parents[1] / "mcp_server" / "bridge.py"
    script = (
        _QT_PRELUDE.format(source=str(source), close_ms=700)
        + """
kind, value, _ = run('request_read_folder')
assert kind == 'ok', (kind, value)
assert value == {'finished': 'request_read_folder'}
"""
    )
    _run_qt(script)

"""Run native Qt lifetime checks in a clean interpreter, outside import stubs."""

import os
from pathlib import Path
import subprocess
import sys
import pytest


def test_bridge_stop_wakes_worker_before_gui_destruction():
    source = Path(__file__).resolve().parents[1] / "mcp_server" / "bridge.py"
    script = f"""
import importlib.util, threading, time
try:
    from PySide6.QtWidgets import QApplication, QWidget
    from PySide6.QtCore import QCoreApplication, QEvent
except ImportError:
    raise SystemExit(77)
spec = importlib.util.spec_from_file_location('real_bridge', {str(source)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
app = QApplication([])
window = QWidget()
bridge = module.MCPBridge(object(), parent=window)
assert bridge.parent() is window
errors = []
def call():
    try:
        bridge.call('delete_atoms', timeout=5)
    except RuntimeError as exc:
        errors.append(str(exc))
worker = threading.Thread(target=call)
worker.start()
deadline = time.monotonic() + 2
while not bridge._pending and time.monotonic() < deadline:
    time.sleep(0.001)
assert bridge._pending
bridge.close()
worker.join(2)
assert not worker.is_alive()
assert errors == ['MCP bridge has stopped']
# The already queued edit must never be executed during event draining.
ran = []
module.execute_operation = lambda *args: ran.append(args)
app.processEvents()
assert ran == []
destroyed = []
bridge.destroyed.connect(lambda: destroyed.append(threading.get_ident()))
bridge.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
assert destroyed == [threading.get_ident()]
"""
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    if result.returncode == 77:
        pytest.skip("PySide6 unavailable")
    assert result.returncode == 0, result.stdout + result.stderr

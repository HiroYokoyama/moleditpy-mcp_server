"""Real Qt tests for the shipped MCP tester's worker and registered slots."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "mcp_gui_tester/src/mcp_gui_tester/app.py"


class WorkerSlotTests(unittest.TestCase):
    def run_qt(self, body):
        prefix = f"""
import gc, importlib.util, threading, time, os
try:
    import PySide6
    from PySide6.QtCore import QThread, Slot
    from PySide6.QtWidgets import QApplication
    from shiboken6 import delete
except ImportError as exc:
    print(exc, file=__import__('sys').stderr)
    raise SystemExit(77)
if os.environ.get('REQUIRED_QT_VERSION'):
    assert PySide6.__version__ == os.environ['REQUIRED_QT_VERSION']
spec = importlib.util.spec_from_file_location('_tester_slots', {str(APP)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
app = QApplication.instance() or QApplication([])
class Window(module.MCPTesterWindow):
    def __init__(self):
        super().__init__('http://127.0.0.1:9/mcp')
        self.results, self.errors, self.threads = [], [], []
    @Slot(object)
    def receive(self, value):
        self.results.append(value)
        self.threads.append(QThread.currentThread())
    @Slot(object)
    def _on_error(self, error):
        self.errors.append(error)
        self.threads.append(QThread.currentThread())
def wait_for(predicate):
    deadline = time.monotonic() + 10
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    assert predicate(), 'worker callback did not arrive'
"""
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", prefix + body],
            capture_output=True,
            text=True,
            timeout=40,
        )
        if result.returncode == 77 and os.environ.get("REQUIRE_REAL_QT") != "1":
            self.skipTest("PySide6 is not installed")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_delayed_results_survive_background_completion(self):
        self.run_qt("""
window = Window()
for i in range(100):
    window._run(lambda value: value, i, on_done=window.receive)
time.sleep(0.2)
gc.collect()
wait_for(lambda: len(window.results) == 100 and not window._workers)
assert sorted(window.results) == list(range(100))
assert all(thread == app.thread() for thread in window.threads)
""")

    def test_failures_release_workers(self):
        self.run_qt("""
window = Window()
def fail():
    raise ValueError('expected failure')
window._run(fail, on_done=window.receive)
wait_for(lambda: bool(window.errors) and not window._workers)
assert window.errors == ['expected failure']
assert window.threads == [app.thread()]
""")

    def test_destroyed_window_drops_late_results(self):
        self.run_qt("""
window = Window()
release = threading.Event()
window._run(lambda: release.wait(5), on_done=window.receive)
results = window.results
delete(window)
release.set()
time.sleep(0.1)
app.processEvents()
assert results == []
""")

    def test_receivers_are_registered_native_slots(self):
        self.run_qt("""
window = Window()
for name in ('_on_connected', '_on_refreshed', '_on_call_done', '_on_error', '_forget_worker'):
    assert window.metaObject().indexOfSlot(name + '(PyObject)') >= 0, name
""")

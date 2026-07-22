"""
Rich, real (subclassable) PyQt6.QtCore stand-ins used only by
test_bridge_qtclass.py to genuinely import mcp_server.bridge and drive
MCPBridge so its statements are actually executed and counted toward
coverage.

The blanket MagicMock PyQt6 mock in conftest.py makes QObject un-subclassable
(subclassing a MagicMock *instance* silently produces another MagicMock, not
a real class), so MCPBridge can't be instantiated normally under it. This
module mirrors the pattern in ui_qt_stubs.py: install real, minimal Qt
stand-ins before importing the module under test.
"""

from __future__ import annotations

import sys
import types

_MODULE_NAMES = ("PyQt6", "PyQt6.QtCore")


class _BoundSignal:
    """A per-instance signal object with synchronous connect/emit."""

    def __init__(self):
        self._fns = []

    def connect(self, fn, type=None):  # noqa: A002 - matches PyQt's kwarg name
        self._fns.append(fn)

    def emit(self, *args, **kwargs):
        for fn in list(self._fns):
            fn(*args, **kwargs)


class pyqtSignal:  # noqa: N801 - matches PyQt's naming
    """Descriptor mimicking PyQt6's per-instance bound-signal behavior."""

    def __init__(self, *types):
        self._types = types
        self._name = None

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        store = instance.__dict__.setdefault("_bound_signals", {})
        if self._name not in store:
            store[self._name] = _BoundSignal()
        return store[self._name]


class QObject:
    def __init__(self, parent=None):
        self._parent = parent


class Qt:
    class ConnectionType:
        QueuedConnection = 1


class QTimer:
    @staticmethod
    def singleShot(ms, fn):  # noqa: N802 - matches PyQt's naming
        fn()


def install_bridge_qt_stubs() -> None:
    """Install real, subclassable PyQt6.QtCore stand-ins into sys.modules."""
    qt_core = types.ModuleType("PyQt6.QtCore")
    qt_core.QObject = QObject
    qt_core.Qt = Qt
    qt_core.QTimer = QTimer
    qt_core.pyqtSignal = pyqtSignal

    pyqt6 = types.ModuleType("PyQt6")
    pyqt6.QtCore = qt_core

    sys.modules["PyQt6"] = pyqt6
    sys.modules["PyQt6.QtCore"] = qt_core


def remove_bridge_qt_stubs() -> None:
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)
    sys.modules.pop("mcp_server.bridge", None)

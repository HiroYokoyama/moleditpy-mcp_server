"""
Shared test infrastructure for moleditpy-mcp_server.

Mocks all heavy optional dependencies so tests run headlessly without
PySide6, RDKit, or a running MoleditPy instance.
"""

from __future__ import annotations

import contextlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

# Keep pytest-qt and Matplotlib on the host's binding.
os.environ.setdefault("PYTEST_QT_API", "pyside6")
os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Initialize PySide's support modules before temporary import stubs can hide
# them; reinitializing the native binding after a stub swap is unsafe.
try:
    import PySide6.QtCore
    import PySide6.QtGui
    import PySide6.QtWidgets
except ImportError:
    pass

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "mcp_server"
HOST_SOURCE = ROOT.parent / "python_molecular_editor" / "moleditpy" / "src"
if HOST_SOURCE.is_dir():
    sys.path.insert(0, str(HOST_SOURCE))

# Add repo root to sys.path so ``import mcp_server`` resolves correctly
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BLOCKED_TOPS: frozenset[str] = frozenset(
    {
        "PySide6",
        "rdkit",
        "pyvista",
        "pyvistaqt",
        "numpy",
        "scipy",
        "moleditpy",
        "vtk",
        "vtkmodules",
    }
)


# ---------------------------------------------------------------------------
# MetaPathFinder that replaces blocked packages with MagicMock
# ---------------------------------------------------------------------------


class _MagicLoader(importlib.abc.Loader):
    def create_module(self, spec: importlib.machinery.ModuleSpec) -> MagicMock:
        m = MagicMock()
        m.__name__ = spec.name
        m.__spec__ = spec
        m.__path__ = []
        m.__package__ = spec.name.split(".")[0]
        if spec.name == "PySide6.QtCore":
            m.Slot = lambda *args, **kwargs: lambda fn: fn
        return m  # type: ignore[return-value]

    def exec_module(self, module: object) -> None:
        pass


class _MagicFinder(importlib.abc.MetaPathFinder):
    _loader = _MagicLoader()

    def find_spec(
        self,
        fullname: str,
        path: object,
        target: object = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname.split(".")[0] in BLOCKED_TOPS:
            return importlib.machinery.ModuleSpec(fullname, self._loader)
        return None


@contextlib.contextmanager
def mock_optional_imports() -> Generator[None, None, None]:
    """Context manager that stubs all optional/heavy imports with MagicMock."""
    removed = {
        k: sys.modules.pop(k)
        for k in list(sys.modules)
        if k.split(".")[0] in BLOCKED_TOPS
    }
    finder = _MagicFinder()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        sys.modules.update(removed)
        for k in list(sys.modules):
            if k.split(".")[0] in BLOCKED_TOPS and k not in removed:
                del sys.modules[k]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_module(rel_path: str) -> object:
    """
    Load a module from *rel_path* (relative to the package root) in isolation.
    Must be called inside a ``mock_optional_imports()`` block.
    """
    path = PKG_DIR / rel_path
    mod_name = f"_test_{path.stem}_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def make_context() -> MagicMock:
    """Return a stub PluginContext with a non-None main window."""
    ctx = MagicMock()
    ctx.get_main_window.return_value = MagicMock()
    ctx.get_setting.return_value = None
    # Like the real host: nothing registered until the plugin registers it.
    ctx.get_window.return_value = None
    return ctx


def make_bridge(operation_results: dict | None = None) -> MagicMock:
    """
    Return a stub MCPBridge whose ``call()`` returns values from
    *operation_results* keyed by operation name.
    """
    bridge = MagicMock()
    results = operation_results or {}

    def _call(operation: str, args: dict | None = None, timeout: float = 10.0):
        if operation in results:
            return results[operation]
        raise KeyError(f"No result configured for operation {operation!r}")

    bridge.call.side_effect = _call
    return bridge

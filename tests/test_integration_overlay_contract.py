"""
Integration: the app internals the compare_structures overlay relies on.

The overlay switches the 3D style and writes carbon colors straight into the
3D manager's override store (the per-atom PluginContext call redraws the
whole molecule each time, which takes seconds for a few dozen atoms). These
are MoleditPy internals, so check against the real package that they are
still there and still shaped as the bridge expects.

Skipped automatically when moleditpy is not installed.
"""
from __future__ import annotations

import inspect
import os

import pytest

os.environ.setdefault("MOLEDITPY_HEADLESS", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

view_3d_logic = pytest.importorskip("moleditpy.ui.view_3d_logic", reason="moleditpy not installed")
constants = pytest.importorskip("moleditpy.utils.constants", reason="moleditpy not installed")
pi = pytest.importorskip("moleditpy.plugins.plugin_interface", reason="moleditpy not installed")


def _manager_class():
    for obj in vars(view_3d_logic).values():
        if inspect.isclass(obj) and hasattr(obj, "set_3d_style") and hasattr(obj, "draw_molecule_3d"):
            return obj
    pytest.fail("no class with set_3d_style/draw_molecule_3d in moleditpy.ui.view_3d_logic")


def test_manager_has_style_switch_and_redraw():
    cls = _manager_class()
    assert list(inspect.signature(cls.set_3d_style).parameters)[1:] == ["style_name"]
    assert callable(cls.draw_molecule_3d)


def test_manager_keeps_override_store_and_style_attrs():
    source = inspect.getsource(_manager_class())
    assert "self._plugin_color_overrides" in source
    assert "self.current_3d_style" in source
    assert "self.current_mol" in source


def test_stick_style_name_and_radius_setting():
    source = inspect.getsource(_manager_class())
    assert '"stick"' in source
    assert "stick_bond_radius" in source


def test_update_override_accepts_none_for_the_fallback_path():
    cls = _manager_class()
    assert "if color_hex is None" in inspect.getsource(cls.update_atom_color_override)
    assert callable(pi.Plugin3DController.set_atom_color)


def test_cpk_table_is_rgb_floats():
    table = constants.CPK_COLORS_PV
    assert isinstance(table, dict)
    for symbol in ("H", "C", "N", "O"):
        rgb = table[symbol]
        assert len(rgb) == 3 and all(0.0 <= float(c) <= 1.0 for c in rgb)

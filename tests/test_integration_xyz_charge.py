"""
Integration: the show_xyz charge handling against MoleditPy's real XYZ loader.

The bridge answers the app's charge prompt by temporarily replacing
``io_manager.prompt_for_charge`` and flipping two settings keys
(``skip_chemistry_checks``, ``always_ask_charge``). Those are the app's
internals, not the PluginContext API, so the mocked unit tests cannot tell
when the app changes them. These tests drive the real ``IOManager`` with
real RDKit bond perception and fail loudly if the contract moves.

Skipped automatically when moleditpy (or RDKit) is not installed.
Install with: pip install moleditpy
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("MOLEDITPY_HEADLESS", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("rdkit", reason="rdkit not installed")
io_logic = pytest.importorskip("moleditpy.ui.io_logic", reason="moleditpy not installed")
pi = pytest.importorskip("moleditpy.plugins.plugin_interface", reason="moleditpy not installed")

WATER = "3\nwater\nO 0.0 0.0 0.117\nH 0.0 0.757 -0.469\nH 0.0 -0.757 -0.469"
HYDROXIDE = "2\nhydroxide\nO 0.0 0.0 0.0\nH 0.0 0.0 0.97"


def _load_bridge():
    path = Path(__file__).resolve().parents[1] / "mcp_server" / "bridge.py"
    spec = importlib.util.spec_from_file_location("_integration_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def bridge():
    return _load_bridge()


@pytest.fixture()
def app(monkeypatch):
    """Real IOManager + real PluginContext over a mocked window shell.

    The class-level prompt_for_charge is replaced by one that fails the test:
    reaching it means the real modal dialog would have opened.
    """
    def _dialog(self):
        raise AssertionError("the app's modal charge dialog would have opened")

    monkeypatch.setattr(io_logic.IOManager, "prompt_for_charge", _dialog)
    settings = {"skip_chemistry_checks": False, "always_ask_charge": False}
    host = MagicMock()
    host.init_manager.settings = settings
    io_mgr = io_logic.IOManager(host)
    mw = MagicMock()
    mw.io_manager = io_mgr
    mw.init_manager.settings = settings
    manager = MagicMock()
    manager.get_main_window.return_value = mw
    ctx = pi.PluginContext(manager, "mcp_server")
    return ctx, io_mgr, settings


def test_contract_prompt_for_charge_exists():
    """The hook the bridge replaces is still where it expects it."""
    assert callable(getattr(io_logic.IOManager, "prompt_for_charge", None))
    assert callable(getattr(io_logic.IOManager, "show_xyz_data", None))


def test_contract_settings_keys_exist():
    from moleditpy.utils.default_settings import DEFAULT_SETTINGS

    assert "skip_chemistry_checks" in DEFAULT_SETTINGS
    assert "always_ask_charge" in DEFAULT_SETTINGS


def test_neutral_loads_with_bond_orders(bridge, app):
    ctx, _, _ = app
    result = bridge.execute_operation(ctx, "show_xyz", {"xyz_text": WATER})
    assert result["success"] is True
    assert result["chemistry_skipped"] is False
    assert result["charge"] == 0
    assert result["num_bonds"] == 2


def test_ion_without_charge_falls_back_instead_of_dialog(bridge, app):
    ctx, _, _ = app
    result = bridge.execute_operation(ctx, "show_xyz", {"xyz_text": HYDROXIDE})
    assert result["success"] is True
    assert result["chemistry_skipped"] is True
    assert "charge" in result["note"]


def test_ion_with_charge_perceives_bonds(bridge, app):
    ctx, _, _ = app
    result = bridge.execute_operation(ctx, "show_xyz", {"xyz_text": HYDROXIDE, "charge": -1})
    assert result["chemistry_skipped"] is False
    assert result["charge"] == -1
    assert ctx.get_main_window().io_manager is not None


def test_wrong_charge_falls_back_once(bridge, app):
    ctx, _, _ = app
    result = bridge.execute_operation(ctx, "show_xyz", {"xyz_text": HYDROXIDE, "charge": 3})
    assert result["success"] is True
    assert result["chemistry_skipped"] is True
    assert "charge 3" in result["note"]


def test_skip_chemistry(bridge, app):
    ctx, _, _ = app
    result = bridge.execute_operation(ctx, "show_xyz", {"xyz_text": WATER, "skip_chemistry": True})
    assert result["chemistry_skipped"] is True
    assert result["num_bonds"] == 2  # distance-based bonds still connect the atoms


def test_state_restored_after_calls(bridge, app):
    ctx, io_mgr, settings = app
    before = dict(settings)
    for args in ({"charge": -1}, {"skip_chemistry": True}, {}):
        bridge.execute_operation(ctx, "show_xyz", dict(args, xyz_text=HYDROXIDE))
    assert settings == before
    assert "prompt_for_charge" not in vars(io_mgr)


def test_trajectory_frame_through_real_loader(bridge, app):
    ctx, _, _ = app
    traj = WATER + "\n" + WATER.replace("0.117", "0.150")
    result = bridge.execute_operation(ctx, "show_xyz", {"xyz_text": traj, "frame": 0})
    assert result["frame"] == 0 and result["num_frames"] == 2
    assert result["num_bonds"] == 2

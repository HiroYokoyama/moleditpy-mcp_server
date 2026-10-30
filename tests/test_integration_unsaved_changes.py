"""
Integration: loading XYZ over MCP after an MCP edit, against MoleditPy's real
unsaved-changes check.

``IOManager.show_xyz_data`` first calls ``state_manager.check_unsaved_changes``,
which opens a modal "Unsaved Changes" box when the document has unsaved
edits. MCP edits (edit_bonds and friends) always leave some, so the bridge
replaces that check for the duration of the call. The check, its inputs
(``has_unsaved_changes``, ``data.atoms``, ``view_3d_manager.current_mol``) and
the call site are the app's internals, not the PluginContext API, so these
tests drive the real ``StateManager`` and ``IOManager`` and fail loudly if the
contract moves.

Skipped automatically when moleditpy (or RDKit) is not installed.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("MOLEDITPY_HEADLESS", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("rdkit", reason="rdkit not installed")
io_logic = pytest.importorskip(
    "moleditpy.ui.io_logic", reason="moleditpy not installed"
)
app_state = pytest.importorskip(
    "moleditpy.ui.app_state", reason="moleditpy not installed"
)
pi = pytest.importorskip(
    "moleditpy.plugins.plugin_interface", reason="moleditpy not installed"
)

from rdkit import Chem
from rdkit.Chem import AllChem

ETHANOL = (
    "9\nethanol\n"
    "C -0.0011 -0.0110 0.0000\n"
    "C 1.5137 0.0120 0.0000\n"
    "O 1.9634 1.3540 0.0000\n"
    "H -0.3829 -1.0390 0.0000\n"
    "H -0.3854 0.4964 0.8889\n"
    "H -0.3854 0.4964 -0.8889\n"
    "H 1.8812 -0.5103 0.8889\n"
    "H 1.8812 -0.5103 -0.8889\n"
    "H 2.9263 1.3433 0.0000"
)


def _load_bridge():
    path = Path(__file__).resolve().parents[1] / "mcp_server" / "bridge.py"
    spec = importlib.util.spec_from_file_location("_integration_unsaved", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def bridge():
    return _load_bridge()


def _benzene():
    mol = Chem.AddHs(Chem.MolFromSmiles("c1ccccc1"))
    AllChem.EmbedMolecule(mol, randomSeed=42)
    return mol


@pytest.fixture()
def app(monkeypatch):
    """Real StateManager + IOManager + PluginContext over a mocked window.

    The dialog the real check would open fails the test instead, and the
    charge prompt is out of the way (benzene/ethanol are neutral anyway).
    """
    asked = []

    def _question(*args, **kwargs):
        asked.append(args)
        raise AssertionError("the app's modal Unsaved Changes box would have opened")

    monkeypatch.setattr(app_state.QMessageBox, "question", _question)
    settings = {"skip_chemistry_checks": False, "always_ask_charge": False}
    mw = MagicMock()
    mw.init_manager.settings = settings
    view_3d = MagicMock()
    view_3d.current_mol = _benzene()
    mw.view_3d_manager = view_3d
    sm = app_state.StateManager(mw)
    sm.data = SimpleNamespace(atoms={})
    mw.state_manager = sm

    def _set_current(mol):
        view_3d.current_mol = mol

    def _set_unsaved(flag):
        sm.has_unsaved_changes = flag

    mw.set_current_molecule.side_effect = _set_current
    mw.set_has_unsaved_changes.side_effect = _set_unsaved
    io_mgr = io_logic.IOManager(mw)
    mw.io_manager = io_mgr

    manager = MagicMock()
    manager.get_main_window.return_value = mw

    def _push_undo():
        # What the app's undo push does to the document: mark it modified.
        sm.has_unsaved_changes = True

    manager.push_undo_checkpoint.side_effect = _push_undo
    ctx = pi.PluginContext(manager, "mcp_server")
    return ctx, sm, view_3d, asked


def test_contract_show_xyz_data_checks_unsaved_changes():
    """The check the bridge replaces is still where it expects it."""
    assert callable(getattr(app_state.StateManager, "check_unsaved_changes", None))
    source = inspect.getsource(io_logic.IOManager.show_xyz_data)
    assert "self.host.state_manager.check_unsaved_changes()" in source


@pytest.mark.parametrize(
    "unsaved,atoms,mol_3d",
    [
        (False, {}, None),
        (False, {1: {}}, object()),
        (True, {}, None),
        (True, {1: {}}, None),
        (True, {}, object()),
    ],
)
def test_contract_unsaved_predicate_matches_app(
    monkeypatch, bridge, unsaved, atoms, mol_3d
):
    """The bridge's "would the app ask?" test agrees with the real check."""
    asked = []

    def _question(*args, **kwargs):
        asked.append(args)
        return app_state.QMessageBox.StandardButton.No

    monkeypatch.setattr(app_state.QMessageBox, "question", _question)
    mw = MagicMock()
    mw.view_3d_manager = SimpleNamespace(current_mol=mol_3d)
    sm = app_state.StateManager(mw)
    sm.data = SimpleNamespace(atoms=atoms)
    sm.has_unsaved_changes = unsaved
    assert sm.check_unsaved_changes() is True
    assert bridge._document_has_unsaved_work(mw, sm) is bool(asked)


def test_edit_bonds_then_load_xyz_returns_error(bridge, app):
    ctx, sm, view_3d, asked = app
    edited = bridge.execute_operation(ctx, "edit_bonds", {"add": [[0, 3]]})
    assert edited["changed"] is True
    assert sm.has_unsaved_changes is True
    before = view_3d.current_mol

    with pytest.raises(ValueError, match="discard_unsaved=true"):
        bridge.execute_operation(ctx, "show_xyz", {"xyz_text": ETHANOL})

    assert asked == []
    assert view_3d.current_mol is before  # nothing was loaded
    assert sm.has_unsaved_changes is True
    assert "check_unsaved_changes" not in vars(sm)  # the app's check is back


def test_edit_bonds_then_load_xyz_with_discard(bridge, app):
    ctx, sm, view_3d, asked = app
    bridge.execute_operation(ctx, "edit_bonds", {"add": [[0, 3]]})

    result = bridge.execute_operation(
        ctx, "show_xyz", {"xyz_text": ETHANOL, "discard_unsaved": True}
    )

    assert asked == []
    assert result["success"] is True
    assert result["num_atoms"] == 9
    assert view_3d.current_mol.GetNumAtoms() == 9
    assert sm.has_unsaved_changes is False
    assert "check_unsaved_changes" not in vars(sm)


def test_load_xyz_without_unsaved_changes_needs_no_flag(bridge, app):
    ctx, sm, view_3d, asked = app
    sm.has_unsaved_changes = False
    result = bridge.execute_operation(ctx, "show_xyz", {"xyz_text": ETHANOL})
    assert asked == []
    assert result["success"] is True

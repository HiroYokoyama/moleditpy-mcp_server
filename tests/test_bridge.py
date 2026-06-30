"""Tests for mcp_server/bridge.py — execute_operation dispatch logic."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from conftest import load_module, make_context, mock_optional_imports


@pytest.fixture()
def bridge_mod():
    """Load bridge.py with heavy deps mocked."""
    with mock_optional_imports():
        yield load_module("bridge.py")


@pytest.fixture()
def ctx():
    return make_context()


# ---------------------------------------------------------------------------
# get_molecule_info
# ---------------------------------------------------------------------------


def test_execute_get_molecule_info_no_mol(bridge_mod, ctx):
    ctx.current_molecule = None
    result = bridge_mod.execute_operation(ctx, "get_molecule_info", {})
    assert result["loaded"] is False
    assert result["smiles"] is None
    assert result["num_atoms"] == 0


def test_execute_get_molecule_info_with_mol(bridge_mod, ctx):
    mock_mol = MagicMock()
    mock_mol.GetNumAtoms.return_value = 9
    mock_mol.GetNumBonds.return_value = 9
    mock_mol.GetNumConformers.return_value = 1
    ctx.current_molecule = mock_mol

    # _get_molecule_info does:
    #   from rdkit import Chem          → chem_mock (local var captured here)
    #   from rdkit.Chem import ...      → Python sets rdkit.Chem = rdkit_chem_mock,
    #                                     but local Chem variable is still chem_mock
    # So configure MolToSmiles on chem_mock and Descriptors/rdMolDescriptors on rdkit_chem_mock.
    chem_mock = MagicMock(name="Chem")
    chem_mock.MolToSmiles.return_value = "c1ccccc1"

    rdkit_mock = MagicMock(name="rdkit")
    rdkit_mock.Chem = chem_mock

    rdkit_chem_mock = MagicMock(name="rdkit.Chem")
    rdkit_chem_mock.Descriptors.MolWt.return_value = 78.11
    rdkit_chem_mock.rdMolDescriptors.CalcMolFormula.return_value = "C6H6"

    saved = {k: sys.modules.get(k) for k in ("rdkit", "rdkit.Chem")}
    sys.modules["rdkit"] = rdkit_mock
    sys.modules["rdkit.Chem"] = rdkit_chem_mock
    try:
        result = bridge_mod.execute_operation(ctx, "get_molecule_info", {})
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    assert result["loaded"] is True
    assert result["smiles"] == "c1ccccc1"
    assert result["formula"] == "C6H6"
    assert result["num_atoms"] == 9
    assert result["has_3d_coords"] is True


# ---------------------------------------------------------------------------
# get_xyz_block
# ---------------------------------------------------------------------------


def test_execute_get_xyz_block_no_data(bridge_mod, ctx):
    ctx.to_xyz_block.return_value = None
    result = bridge_mod.execute_operation(ctx, "get_xyz_block", {})
    assert result["has_data"] is False
    assert result["xyz_block"] is None


def test_execute_get_xyz_block_with_data(bridge_mod, ctx):
    xyz = "C  0.0  0.0  0.0"
    ctx.to_xyz_block.return_value = xyz
    result = bridge_mod.execute_operation(ctx, "get_xyz_block", {})
    assert result["has_data"] is True
    assert result["xyz_block"] == xyz


# ---------------------------------------------------------------------------
# load_smiles
# ---------------------------------------------------------------------------


def test_execute_load_smiles_ok(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "CCO"})
    ctx.load_from_smiles.assert_called_once_with("CCO")
    assert result["success"] is True


def test_execute_load_smiles_empty_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="required"):
        bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": ""})


def test_execute_load_smiles_strips_whitespace(bridge_mod, ctx):
    bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "  CCO  "})
    ctx.load_from_smiles.assert_called_once_with("CCO")


# ---------------------------------------------------------------------------
# show_xyz
# ---------------------------------------------------------------------------


def test_execute_show_xyz_success(bridge_mod, ctx):
    ctx.show_xyz_data.return_value = MagicMock()
    result = bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": "C 0 0 0"})
    assert result["success"] is True
    ctx.show_xyz_data.assert_called_once_with("C 0 0 0", source_name="MCP input")


def test_execute_show_xyz_with_source_name(bridge_mod, ctx):
    ctx.show_xyz_data.return_value = MagicMock()
    bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": "C 0 0 0", "source_name": "ORCA"}
    )
    ctx.show_xyz_data.assert_called_once_with("C 0 0 0", source_name="ORCA")


def test_execute_show_xyz_failure(bridge_mod, ctx):
    ctx.show_xyz_data.return_value = None
    result = bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": "invalid data"})
    assert result["success"] is False


def test_execute_show_xyz_empty_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="required"):
        bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": "  "})


# ---------------------------------------------------------------------------
# get_atom_properties
# ---------------------------------------------------------------------------


def test_execute_get_atom_properties_no_mol(bridge_mod, ctx):
    ctx.current_molecule = None
    result = bridge_mod.execute_operation(ctx, "get_atom_properties", {"atom_indices": [0]})
    assert result["atoms"] == []


def test_execute_get_atom_properties_all_atoms(bridge_mod, ctx):
    mock_mol = MagicMock()
    mock_mol.GetNumAtoms.return_value = 2
    atom_c = MagicMock()
    atom_c.GetSymbol.return_value = "C"
    atom_c.GetAtomicNum.return_value = 6
    atom_c.GetFormalCharge.return_value = 0
    atom_c.GetHybridization.return_value = "SP3"
    atom_c.GetTotalNumHs.return_value = 4
    atom_c.GetNumRadicalElectrons.return_value = 0
    mock_mol.GetAtomWithIdx.return_value = atom_c
    ctx.current_molecule = mock_mol

    result = bridge_mod.execute_operation(ctx, "get_atom_properties", {})
    assert len(result["atoms"]) == 2
    assert result["atoms"][0]["symbol"] == "C"
    assert result["atoms"][0]["formal_charge"] == 0


def test_execute_get_atom_properties_specific_indices(bridge_mod, ctx):
    mock_mol = MagicMock()
    mock_mol.GetNumAtoms.return_value = 5
    atom = MagicMock()
    atom.GetSymbol.return_value = "N"
    atom.GetAtomicNum.return_value = 7
    atom.GetFormalCharge.return_value = 1
    atom.GetHybridization.return_value = "SP2"
    atom.GetTotalNumHs.return_value = 0
    atom.GetNumRadicalElectrons.return_value = 0
    mock_mol.GetAtomWithIdx.return_value = atom
    ctx.current_molecule = mock_mol

    result = bridge_mod.execute_operation(ctx, "get_atom_properties", {"atom_indices": [2]})
    assert len(result["atoms"]) == 1
    assert result["atoms"][0]["index"] == 2
    assert result["atoms"][0]["symbol"] == "N"


# ---------------------------------------------------------------------------
# get_bond_info
# ---------------------------------------------------------------------------


def test_execute_get_bond_info_no_mol(bridge_mod, ctx):
    ctx.current_molecule = None
    result = bridge_mod.execute_operation(ctx, "get_bond_info", {})
    assert result["bonds"] == []


def test_execute_get_bond_info_with_bonds(bridge_mod, ctx):
    mock_mol = MagicMock()
    bond = MagicMock()
    bond.GetIdx.return_value = 0
    bond.GetBeginAtomIdx.return_value = 0
    bond.GetEndAtomIdx.return_value = 1
    bond.GetBondTypeAsDouble.return_value = 2.0
    mock_mol.GetBonds.return_value = [bond]
    ctx.current_molecule = mock_mol

    result = bridge_mod.execute_operation(ctx, "get_bond_info", {})
    assert len(result["bonds"]) == 1
    b = result["bonds"][0]
    assert b["atom1"] == 0
    assert b["atom2"] == 1
    assert b["bond_type"] == "DOUBLE"


def test_execute_get_bond_info_aromatic(bridge_mod, ctx):
    mock_mol = MagicMock()
    bond = MagicMock()
    bond.GetIdx.return_value = 0
    bond.GetBeginAtomIdx.return_value = 0
    bond.GetEndAtomIdx.return_value = 1
    bond.GetBondTypeAsDouble.return_value = 1.5
    mock_mol.GetBonds.return_value = [bond]
    ctx.current_molecule = mock_mol

    result = bridge_mod.execute_operation(ctx, "get_bond_info", {})
    assert result["bonds"][0]["bond_type"] == "AROMATIC"


# ---------------------------------------------------------------------------
# load_mol_block
# ---------------------------------------------------------------------------


def test_execute_load_mol_block_ok(bridge_mod, ctx):
    mol_mock = MagicMock()
    chem_mock = MagicMock(name="Chem")
    chem_mock.MolFromMolBlock.return_value = mol_mock
    rdkit_mock = MagicMock(name="rdkit")
    rdkit_mock.Chem = chem_mock
    saved = {k: sys.modules.get(k) for k in ("rdkit", "rdkit.Chem")}
    sys.modules["rdkit"] = rdkit_mock
    sys.modules["rdkit.Chem"] = chem_mock
    try:
        result = bridge_mod.execute_operation(
            ctx, "load_mol_block", {"mol_block": "\n  Mrv2211\n\n..."}
        )
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    assert result["success"] is True
    assert ctx.current_molecule == mol_mock
    ctx.push_undo_checkpoint.assert_called()
    ctx.refresh_ui.assert_called()


def test_execute_load_mol_block_empty_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="required"):
        bridge_mod.execute_operation(ctx, "load_mol_block", {"mol_block": ""})


def test_execute_load_mol_block_parse_failure(bridge_mod, ctx):
    chem_mock = MagicMock(name="Chem")
    chem_mock.MolFromMolBlock.return_value = None
    rdkit_mock = MagicMock(name="rdkit")
    rdkit_mock.Chem = chem_mock
    saved = {k: sys.modules.get(k) for k in ("rdkit", "rdkit.Chem")}
    sys.modules["rdkit"] = rdkit_mock
    sys.modules["rdkit.Chem"] = chem_mock
    try:
        result = bridge_mod.execute_operation(ctx, "load_mol_block", {"mol_block": "garbage"})
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    assert result["success"] is False


# ---------------------------------------------------------------------------
# trigger_3d_conversion
# ---------------------------------------------------------------------------


def test_execute_trigger_3d_conversion_via_compute_manager(bridge_mod, ctx):
    mw = ctx.get_main_window.return_value
    cm = MagicMock()
    mw.compute_manager = cm
    result = bridge_mod.execute_operation(ctx, "trigger_3d_conversion", {})
    cm.trigger_conversion.assert_called_once()
    assert result["success"] is True


def test_execute_trigger_3d_conversion_fallback_rdkit(bridge_mod, ctx):
    # No compute_manager on main window → fallback to RDKit ETKDG
    mw = MagicMock(spec=[])  # spec=[] means no attributes match hasattr
    ctx.get_main_window.return_value = mw

    allchem_mock = MagicMock(name="AllChem")
    allchem_mock.EmbedMolecule.return_value = 0  # success
    allchem_mock.ETKDGv3.return_value = MagicMock()

    mol_mock = MagicMock()
    ctx.current_molecule = mol_mock

    chem_mock = MagicMock(name="Chem")
    chem_mock.AddHs.return_value = mol_mock
    chem_mock.AllChem = allchem_mock

    rdkit_mock = MagicMock(name="rdkit")
    rdkit_mock.Chem = chem_mock

    saved = {k: sys.modules.get(k) for k in ("rdkit", "rdkit.Chem", "rdkit.Chem.AllChem")}
    sys.modules["rdkit"] = rdkit_mock
    sys.modules["rdkit.Chem"] = chem_mock
    sys.modules["rdkit.Chem.AllChem"] = allchem_mock
    try:
        result = bridge_mod.execute_operation(ctx, "trigger_3d_conversion", {})
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    assert result["success"] is True
    ctx.enter_3d_viewer_mode.assert_called()
    ctx.refresh_ui.assert_called()


# ---------------------------------------------------------------------------
# highlight_atoms
# ---------------------------------------------------------------------------


def test_execute_highlight_atoms_ok(bridge_mod, ctx):
    colors = {"0": "#FF0000", "3": "#00FF00"}
    result = bridge_mod.execute_operation(ctx, "highlight_atoms", {"atom_colors": colors})
    ctrl = ctx.get_3d_controller.return_value
    assert ctrl.set_atom_color.call_count == 2
    ctx.refresh_3d_view.assert_called()
    assert result["success"] is True


def test_execute_highlight_atoms_empty_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="required"):
        bridge_mod.execute_operation(ctx, "highlight_atoms", {"atom_colors": {}})


# ---------------------------------------------------------------------------
# highlight_bonds
# ---------------------------------------------------------------------------


def test_execute_highlight_bonds_ok(bridge_mod, ctx):
    colors = {"0": "#FF0000", "2": "#0000FF"}
    result = bridge_mod.execute_operation(ctx, "highlight_bonds", {"bond_colors": colors})
    ctrl = ctx.get_3d_controller.return_value
    assert ctrl.set_bond_color.call_count == 2
    ctx.refresh_3d_view.assert_called()
    assert result["success"] is True


def test_execute_highlight_bonds_empty_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="required"):
        bridge_mod.execute_operation(ctx, "highlight_bonds", {"bond_colors": {}})


# ---------------------------------------------------------------------------
# push_undo_checkpoint / enter_3d_mode / fit_3d_view / reset_3d_camera
# ---------------------------------------------------------------------------


def test_execute_push_undo_checkpoint(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "push_undo_checkpoint", {})
    ctx.push_undo_checkpoint.assert_called_once()
    assert result["success"] is True


def test_execute_enter_3d_mode(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "enter_3d_mode", {})
    ctx.enter_3d_viewer_mode.assert_called_once()
    assert result["success"] is True


def test_execute_fit_3d_view(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "fit_3d_view", {})
    ctx.fit_3d_view.assert_called_once()
    assert result["success"] is True


def test_execute_reset_3d_camera(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "reset_3d_camera", {})
    ctx.reset_3d_camera.assert_called_once()
    assert result["success"] is True


def test_execute_refresh_3d_view(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "refresh_3d_view", {})
    ctx.refresh_3d_view.assert_called_once()
    assert result["success"] is True


# ---------------------------------------------------------------------------
# check_chemistry / refresh_ui
# ---------------------------------------------------------------------------


def test_execute_check_chemistry(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "check_chemistry", {})
    ctx.check_chemistry_problems.assert_called_once()
    ctx.refresh_ui.assert_called()
    assert result["success"] is True


def test_execute_refresh_ui(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "refresh_ui", {})
    ctx.refresh_ui.assert_called_once()
    assert result["success"] is True


# ---------------------------------------------------------------------------
# run_python
# ---------------------------------------------------------------------------


def test_execute_run_python_stdout(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "run_python", {"code": "print('hello')"})
    assert "hello" in result["stdout"]
    assert result["stderr"] == ""


def test_execute_run_python_result(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "run_python", {"code": "result = 1 + 2"})
    assert result["result"] == "3"


def test_execute_run_python_ctx_access(bridge_mod, ctx):
    result = bridge_mod.execute_operation(
        ctx, "run_python", {"code": "result = ctx is not None"}
    )
    assert result["result"] == "True"


def test_execute_run_python_stderr(bridge_mod, ctx):
    result = bridge_mod.execute_operation(
        ctx, "run_python", {"code": "import sys; print('err', file=sys.stderr)"}
    )
    assert "err" in result["stderr"]


def test_execute_run_python_empty_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="required"):
        bridge_mod.execute_operation(ctx, "run_python", {"code": ""})


# ---------------------------------------------------------------------------
# get_selected_atoms
# ---------------------------------------------------------------------------


def test_execute_get_selected_atoms_empty(bridge_mod, ctx):
    ctx.get_selected_atom_indices.return_value = []
    result = bridge_mod.execute_operation(ctx, "get_selected_atoms", {})
    assert result["count"] == 0
    assert result["selected_atoms"] == []


def test_execute_get_selected_atoms_with_mol(bridge_mod, ctx):
    mock_mol = MagicMock()
    atom_c = MagicMock()
    atom_c.GetSymbol.return_value = "C"
    atom_c.GetAtomicNum.return_value = 6
    atom_o = MagicMock()
    atom_o.GetSymbol.return_value = "O"
    atom_o.GetAtomicNum.return_value = 8
    mock_mol.GetAtomWithIdx.side_effect = lambda i: {0: atom_c, 1: atom_o}[i]

    ctx.get_selected_atom_indices.return_value = [0, 1]
    ctx.current_molecule = mock_mol

    result = bridge_mod.execute_operation(ctx, "get_selected_atoms", {})
    assert result["count"] == 2
    symbols = [a["symbol"] for a in result["selected_atoms"]]
    assert "C" in symbols
    assert "O" in symbols


# ---------------------------------------------------------------------------
# clear_canvas
# ---------------------------------------------------------------------------


def test_execute_clear_canvas(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "clear_canvas", {})
    ctx.clear_canvas.assert_called_once_with(push_to_undo=True)
    assert result["success"] is True


# ---------------------------------------------------------------------------
# get_app_info
# ---------------------------------------------------------------------------


def test_execute_get_app_info_with_version(bridge_mod, ctx):
    mw = ctx.get_main_window.return_value
    mw.VERSION = "4.1.0"

    # Ensure mcp_server is visible for the import inside _get_app_info
    saved = sys.modules.get("mcp_server")
    fake = types.ModuleType("mcp_server")
    fake.PLUGIN_VERSION = "0.1.0"
    sys.modules["mcp_server"] = fake
    try:
        result = bridge_mod.execute_operation(ctx, "get_app_info", {})
    finally:
        if saved is None:
            sys.modules.pop("mcp_server", None)
        else:
            sys.modules["mcp_server"] = saved

    assert result["app"] == "MoleditPy"
    assert result["version"] == "4.1.0"
    assert result["mcp_plugin_version"] == "0.1.0"


# ---------------------------------------------------------------------------
# get_file_io_config
# ---------------------------------------------------------------------------


def test_get_file_io_config_no_setting(bridge_mod, ctx):
    ctx.get_setting.side_effect = lambda key, default=None: default
    result = bridge_mod.execute_operation(ctx, "get_file_io_config", {})
    assert result["base_dir"] is None
    assert isinstance(result["allowed_extensions"], list)
    assert ".inp" in result["allowed_extensions"]


def test_get_file_io_config_with_setting(bridge_mod, ctx):
    ctx.get_setting.side_effect = lambda key, default=None: {
        "file_io_base_dir": "/home/user/calc",
        "file_io_allowed_extensions": [".xyz", ".inp"],
    }.get(key, default)
    result = bridge_mod.execute_operation(ctx, "get_file_io_config", {})
    assert result["base_dir"] == "/home/user/calc"
    assert ".xyz" in result["allowed_extensions"]


# ---------------------------------------------------------------------------
# set_file_io_config
# ---------------------------------------------------------------------------


def test_set_file_io_config_base_dir(bridge_mod, ctx):
    result = bridge_mod.execute_operation(
        ctx, "set_file_io_config", {"base_dir": "/tmp/calc"}
    )
    assert result["success"] is True
    ctx.set_setting.assert_any_call("file_io_base_dir", "/tmp/calc")


def test_set_file_io_config_extensions(bridge_mod, ctx):
    bridge_mod.execute_operation(
        ctx, "set_file_io_config", {"allowed_extensions": [".inp", "xyz"]}
    )
    call_args = ctx.set_setting.call_args_list
    ext_call = next(c for c in call_args if c.args[0] == "file_io_allowed_extensions")
    exts = ext_call.args[1]
    assert ".inp" in exts
    assert ".xyz" in exts  # bare "xyz" should be normalized to ".xyz"


def test_set_file_io_config_shows_status(bridge_mod, ctx):
    bridge_mod.execute_operation(
        ctx, "set_file_io_config", {"base_dir": "/tmp/calc"}
    )
    ctx.show_status_message.assert_called_once()


# ---------------------------------------------------------------------------
# unknown operation
# ---------------------------------------------------------------------------


def test_execute_unknown_operation_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="Unknown operation"):
        bridge_mod.execute_operation(ctx, "does_not_exist", {})

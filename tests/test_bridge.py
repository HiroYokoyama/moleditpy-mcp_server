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
    ctx.load_from_mol_block.return_value = MagicMock()
    result = bridge_mod.execute_operation(ctx, "load_mol_block", {"mol_block": "\n  Mrv2211\n\n..."})
    assert result["success"] is True
    ctx.load_from_mol_block.assert_called_once()


def test_execute_load_mol_block_empty_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="required"):
        bridge_mod.execute_operation(ctx, "load_mol_block", {"mol_block": ""})


def test_execute_load_mol_block_parse_failure(bridge_mod, ctx):
    ctx.load_from_mol_block.return_value = None
    result = bridge_mod.execute_operation(ctx, "load_mol_block", {"mol_block": "garbage"})
    assert result["success"] is False


# ---------------------------------------------------------------------------
# trigger_3d_conversion
# ---------------------------------------------------------------------------


def test_execute_trigger_3d_conversion(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "trigger_3d_conversion", {})
    ctx.generate_3d_coords.assert_called_once()
    assert result["success"] is True


# ---------------------------------------------------------------------------
# highlight_atoms
# ---------------------------------------------------------------------------


def test_execute_highlight_atoms_ok(bridge_mod, ctx):
    colors = {"0": "#FF0000", "3": "#00FF00"}
    result = bridge_mod.execute_operation(ctx, "highlight_atoms", {"atom_colors": colors})
    ctx.set_atom_colors.assert_called_once_with(colors)
    assert result["success"] is True


def test_execute_highlight_atoms_empty_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="required"):
        bridge_mod.execute_operation(ctx, "highlight_atoms", {"atom_colors": {}})


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
# unknown operation
# ---------------------------------------------------------------------------


def test_execute_unknown_operation_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="Unknown operation"):
        bridge_mod.execute_operation(ctx, "does_not_exist", {})

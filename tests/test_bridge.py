"""Tests for mcp_server/bridge.py — execute_operation dispatch logic."""

from __future__ import annotations

import sys
import types
from pathlib import Path
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
# get_xyz_atoms
# ---------------------------------------------------------------------------


def test_execute_get_xyz_atoms_no_mol(bridge_mod, ctx):
    ctx.current_molecule = None
    result = bridge_mod.execute_operation(ctx, "get_xyz_atoms", {})
    assert result["has_data"] is False
    assert result["atoms"] == []


def test_execute_get_xyz_atoms_no_conformer(bridge_mod, ctx):
    mock_mol = MagicMock()
    mock_mol.GetNumConformers.return_value = 0
    ctx.current_molecule = mock_mol
    result = bridge_mod.execute_operation(ctx, "get_xyz_atoms", {})
    assert result["has_data"] is False
    assert result["atoms"] == []


def _fake_xyz_atom(idx, symbol, z):
    atom = MagicMock()
    atom.GetIdx.return_value = idx
    atom.GetSymbol.return_value = symbol
    atom.GetAtomicNum.return_value = z
    return atom


def test_execute_get_xyz_atoms_with_data(bridge_mod, ctx):
    mock_mol = MagicMock()
    mock_mol.GetNumConformers.return_value = 1
    mock_mol.GetAtoms.return_value = [
        _fake_xyz_atom(0, "C", 6),
        _fake_xyz_atom(1, "O", 8),
    ]
    positions = {
        0: MagicMock(x=0.0, y=0.5, z=-1.25),
        1: MagicMock(x=1.2, y=0.0, z=0.0),
    }
    mock_mol.GetConformer.return_value.GetAtomPosition.side_effect = positions.__getitem__
    ctx.current_molecule = mock_mol

    result = bridge_mod.execute_operation(ctx, "get_xyz_atoms", {})
    assert result["has_data"] is True
    assert len(result["atoms"]) == 2
    assert result["atoms"][0] == {
        "index": 0, "symbol": "C", "atomic_num": 6,
        "x": 0.0, "y": 0.5, "z": -1.25,
    }
    assert result["atoms"][1]["symbol"] == "O"
    assert result["atoms"][1]["x"] == 1.2


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
# apply_reaction_smarts
# ---------------------------------------------------------------------------


class _RdkitPatch:
    """Install configured rdkit/rdkit.Chem/rdkit.Chem.AllChem mocks in sys.modules."""

    def __init__(self):
        self.chem = MagicMock(name="Chem")
        self.allchem = MagicMock(name="AllChem")
        self.chem.AllChem = self.allchem
        rdkit = MagicMock(name="rdkit")
        rdkit.Chem = self.chem
        self._modules = {
            "rdkit": rdkit,
            "rdkit.Chem": self.chem,
            "rdkit.Chem.AllChem": self.allchem,
        }
        self._saved = {}

    def __enter__(self):
        self._saved = {k: sys.modules.get(k) for k in self._modules}
        sys.modules.update(self._modules)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        return False


def _make_product(num_atoms: int) -> MagicMock:
    prod = MagicMock()
    prod.GetNumAtoms.return_value = num_atoms
    prod.GetAtoms.return_value = []
    return prod


def _configure_reaction(rd, mol, products, final_smiles="CCO", clean_atoms=None):
    """Wire a fake successful reaction pipeline through the rdkit mocks."""
    mol_h = MagicMock(name="mol_with_h")
    rd.chem.AddHs.return_value = mol_h
    rxn = rd.allchem.ReactionFromSmarts.return_value
    rxn.RunReactants.return_value = products
    rd.chem.RemoveHs.side_effect = lambda m, **kw: m
    clean = MagicMock(name="clean_mol")
    clean.GetAtoms.return_value = []
    clean.GetNumHeavyAtoms.return_value = (
        clean_atoms if clean_atoms is not None else mol.GetNumHeavyAtoms.return_value
    )
    rd.chem.MolFromSmiles.return_value = clean
    rd.chem.MolToSmiles.return_value = final_smiles
    return mol_h, rxn


def test_apply_reaction_smarts_empty_raises(bridge_mod, ctx):
    with _RdkitPatch():
        with pytest.raises(ValueError, match="required"):
            bridge_mod.execute_operation(ctx, "apply_reaction_smarts", {"reaction_smarts": " "})


def test_apply_reaction_smarts_no_molecule_raises(bridge_mod, ctx):
    ctx.current_molecule = None
    with _RdkitPatch():
        with pytest.raises(ValueError, match="No molecule"):
            bridge_mod.execute_operation(
                ctx, "apply_reaction_smarts", {"reaction_smarts": "[c:1][H]>>[c:1][Cl]"}
            )


def test_apply_reaction_smarts_invalid_smarts_raises(bridge_mod, ctx):
    ctx.current_molecule = MagicMock()
    with _RdkitPatch() as rd:
        rd.allchem.ReactionFromSmarts.side_effect = RuntimeError("bad smarts")
        with pytest.raises(ValueError, match="Invalid reaction SMARTS"):
            bridge_mod.execute_operation(
                ctx, "apply_reaction_smarts", {"reaction_smarts": "garbage"}
            )


def test_apply_reaction_smarts_no_match_raises(bridge_mod, ctx):
    ctx.current_molecule = MagicMock()
    with _RdkitPatch() as rd:
        rxn = rd.allchem.ReactionFromSmarts.return_value
        rxn.RunReactants.return_value = ()
        with pytest.raises(ValueError, match="did not match"):
            bridge_mod.execute_operation(
                ctx, "apply_reaction_smarts", {"reaction_smarts": "[N:1][H]>>[N:1]C"}
            )


def test_apply_reaction_smarts_success(bridge_mod, ctx):
    mol = MagicMock()
    mol.GetNumHeavyAtoms.return_value = 6
    ctx.current_molecule = mol
    with _RdkitPatch() as rd:
        _configure_reaction(rd, mol, ((_make_product(7),),), final_smiles="Clc1ccccc1")
        result = bridge_mod.execute_operation(
            ctx, "apply_reaction_smarts", {"reaction_smarts": "[c:1][H]>>[c:1][Cl]"}
        )
    assert result["success"] is True
    assert result["smiles"] == "Clc1ccccc1"
    assert result["num_products"] == 1
    ctx.clear_canvas.assert_called_once_with(push_to_undo=False)
    ctx.load_from_smiles.assert_called_once_with("Clc1ccccc1")
    ctx.push_undo_checkpoint.assert_called_once()
    ctx.refresh_ui.assert_called_once()
    # 2D->3D conversion runs by default so chained applies keep a molecule
    assert result["converted_3d"] is True
    ctx.get_main_window.return_value.compute_manager.trigger_conversion.assert_called_once()
    # fresh atom mapping is reported for chained targeting
    assert result["mapped_smiles"] == "Clc1ccccc1"


def test_apply_reaction_smarts_convert_to_3d_opt_out(bridge_mod, ctx):
    mol = MagicMock()
    mol.GetNumHeavyAtoms.return_value = 6
    ctx.current_molecule = mol
    with _RdkitPatch() as rd:
        _configure_reaction(rd, mol, ((_make_product(7),),), final_smiles="Clc1ccccc1")
        result = bridge_mod.execute_operation(
            ctx,
            "apply_reaction_smarts",
            {"reaction_smarts": "[c:1][H]>>[c:1][Cl]", "convert_to_3d": False},
        )
    assert result["converted_3d"] is False
    ctx.get_main_window.return_value.compute_manager.trigger_conversion.assert_not_called()
    ctx.load_from_smiles.assert_called_once_with("Clc1ccccc1")


def test_apply_reaction_smarts_invalid_product_raises(bridge_mod, ctx):
    mol = MagicMock()
    mol.GetNumHeavyAtoms.return_value = 6
    ctx.current_molecule = mol
    with _RdkitPatch() as rd:
        _configure_reaction(rd, mol, ((_make_product(7),),))
        rd.chem.MolFromSmiles.return_value = None
        with pytest.raises(ValueError, match="invalid molecule"):
            bridge_mod.execute_operation(
                ctx, "apply_reaction_smarts", {"reaction_smarts": "[c:1][H]>>[c:1][Cl]"}
            )
    ctx.clear_canvas.assert_not_called()
    ctx.load_from_smiles.assert_not_called()


def test_apply_reaction_smarts_atom_loss_guard(bridge_mod, ctx):
    mol = MagicMock()
    mol.GetNumHeavyAtoms.return_value = 20
    ctx.current_molecule = mol
    with _RdkitPatch() as rd:
        _configure_reaction(rd, mol, ((_make_product(3),),), clean_atoms=3)
        with pytest.raises(ValueError, match="atom loss"):
            bridge_mod.execute_operation(
                ctx, "apply_reaction_smarts", {"reaction_smarts": "[c:1][H]>>[c:1][Cl]"}
            )
    ctx.clear_canvas.assert_not_called()
    ctx.load_from_smiles.assert_not_called()


def test_apply_reaction_smarts_guard_ignores_explicit_hydrogens(bridge_mod, ctx):
    """Regression: editor mol with explicit Hs (benzene = 12 atoms, 6 heavy)
    transformed to chlorobenzene (7 heavy) must NOT trip the atom-loss guard —
    comparing 12 total against 7 heavy previously aborted valid edits."""
    mol = MagicMock()
    mol.GetNumAtoms.return_value = 12
    mol.GetNumHeavyAtoms.return_value = 6
    ctx.current_molecule = mol
    with _RdkitPatch() as rd:
        _configure_reaction(
            rd, mol, ((_make_product(7),),), final_smiles="Clc1ccccc1", clean_atoms=7
        )
        result = bridge_mod.execute_operation(
            ctx, "apply_reaction_smarts", {"reaction_smarts": "[c:1][H]>>[c:1][Cl]"}
        )
    assert result["success"] is True
    ctx.load_from_smiles.assert_called_once_with("Clc1ccccc1")


def test_apply_reaction_smarts_anchor_selects_matching_site(bridge_mod, ctx):
    mol = MagicMock()
    mol.GetNumHeavyAtoms.return_value = 6
    ctx.current_molecule = mol
    products = ((_make_product(7),), (_make_product(7),))
    with _RdkitPatch() as rd:
        mol_h, rxn = _configure_reaction(rd, mol, products)
        mol_h.GetSubstructMatches.return_value = ((0, 1), (4, 5))
        result = bridge_mod.execute_operation(
            ctx,
            "apply_reaction_smarts",
            {"reaction_smarts": "[c:1][H]>>[c:1][Cl]", "atom_index": 4},
        )
    assert result["selected_product"] == 1
    assert result["num_products"] == 2


def test_apply_reaction_smarts_anchor_not_found_falls_back(bridge_mod, ctx):
    mol = MagicMock()
    mol.GetNumHeavyAtoms.return_value = 6
    ctx.current_molecule = mol
    products = ((_make_product(7),), (_make_product(7),))
    with _RdkitPatch() as rd:
        mol_h, _ = _configure_reaction(rd, mol, products)
        mol_h.GetSubstructMatches.return_value = ((0, 1), (4, 5))
        result = bridge_mod.execute_operation(
            ctx,
            "apply_reaction_smarts",
            {"reaction_smarts": "[c:1][H]>>[c:1][Cl]", "atom_index": 99},
        )
    assert result["selected_product"] == 0


def test_apply_reaction_smarts_anchor_more_matches_than_products_breaks(bridge_mod, ctx):
    """More RunReactants matches than products (i >= len(products)) must
    break the enumeration loop instead of indexing out of range."""
    mol = MagicMock()
    mol.GetNumHeavyAtoms.return_value = 6
    ctx.current_molecule = mol
    products = ((_make_product(7),), (_make_product(7),))  # only 2 products
    with _RdkitPatch() as rd:
        mol_h, _ = _configure_reaction(rd, mol, products)
        # 3 matches, none containing the anchor, so the loop runs past
        # len(products) and must break rather than IndexError.
        mol_h.GetSubstructMatches.return_value = ((0, 1), (2, 3), (4, 5))
        result = bridge_mod.execute_operation(
            ctx,
            "apply_reaction_smarts",
            {"reaction_smarts": "[c:1][H]>>[c:1][Cl]", "atom_index": 99},
        )
    assert result["selected_product"] == 0


def test_apply_reaction_smarts_anchor_bad_atom_index_falls_back(bridge_mod, ctx):
    """A non-integer atom_index must be caught by the broad except and fall
    back to the first match instead of propagating."""
    mol = MagicMock()
    mol.GetNumHeavyAtoms.return_value = 6
    ctx.current_molecule = mol
    products = ((_make_product(7),),)
    with _RdkitPatch() as rd:
        _configure_reaction(rd, mol, products)
        result = bridge_mod.execute_operation(
            ctx,
            "apply_reaction_smarts",
            {"reaction_smarts": "[c:1][H]>>[c:1][Cl]", "atom_index": "not-a-number"},
        )
    assert result["selected_product"] == 0


def test_apply_reaction_smarts_sanitize_and_removehs_failures_continue(bridge_mod, ctx):
    """SanitizeMol/RemoveHs failures on the product are logged and swallowed
    (not fatal) — the transformation still succeeds. Also exercises the
    atom-map-reset loop body (new_mol.GetAtoms() non-empty) and the
    mapped_smiles atom-map loop body (report_mol.GetAtoms() non-empty)."""
    mol = MagicMock()
    mol.GetNumHeavyAtoms.return_value = 6
    ctx.current_molecule = mol
    with _RdkitPatch() as rd:
        atom_in_product = MagicMock()
        product = MagicMock()
        product.GetNumAtoms.return_value = 7
        product.GetAtoms.return_value = [atom_in_product]
        _configure_reaction(rd, mol, ((product,),), final_smiles="Clc1ccccc1")
        rd.chem.SanitizeMol.side_effect = RuntimeError("sanitize fail")
        rd.chem.RemoveHs.side_effect = RuntimeError("removehs fail")
        report_atom = MagicMock()
        report_atom.GetIdx.return_value = 0
        rd.chem.MolFromSmiles.return_value.GetAtoms.return_value = [report_atom]

        result = bridge_mod.execute_operation(
            ctx, "apply_reaction_smarts", {"reaction_smarts": "[c:1][H]>>[c:1][Cl]"}
        )
    assert result["success"] is True
    atom_in_product.SetAtomMapNum.assert_called_once_with(0)
    report_atom.SetAtomMapNum.assert_called_once_with(1)


def test_apply_reaction_smarts_3d_conversion_failure_continues(bridge_mod, ctx):
    """A failure in the post-transformation 3D conversion must be logged
    and swallowed — the overall transformation still reports success, with
    converted_3d = False."""
    mol = MagicMock()
    mol.GetNumHeavyAtoms.return_value = 6
    ctx.current_molecule = mol
    ctx.get_main_window.return_value.compute_manager.trigger_conversion.side_effect = (
        RuntimeError("3d fail")
    )
    with _RdkitPatch() as rd:
        _configure_reaction(rd, mol, ((_make_product(7),),), final_smiles="Clc1ccccc1")
        result = bridge_mod.execute_operation(
            ctx, "apply_reaction_smarts", {"reaction_smarts": "[c:1][H]>>[c:1][Cl]"}
        )
    assert result["success"] is True
    assert result["converted_3d"] is False


# ---------------------------------------------------------------------------
# get_mapped_smiles
# ---------------------------------------------------------------------------


def test_get_mapped_smiles_no_molecule(bridge_mod, ctx):
    ctx.current_molecule = None
    with _RdkitPatch():
        result = bridge_mod.execute_operation(ctx, "get_mapped_smiles", {})
    assert result["loaded"] is False
    assert result["mapped_smiles"] is None
    assert result["atoms"] == []


def test_get_mapped_smiles_maps_index_plus_one(bridge_mod, ctx):
    mol = MagicMock()
    ctx.current_molecule = mol
    atom0 = MagicMock()
    atom0.GetIdx.return_value = 0
    atom0.GetSymbol.return_value = "C"
    atom1 = MagicMock()
    atom1.GetIdx.return_value = 1
    atom1.GetSymbol.return_value = "O"
    with _RdkitPatch() as rd:
        tagged = rd.chem.Mol.return_value
        tagged.GetAtoms.return_value = [atom0, atom1]
        rd.chem.MolToSmiles.return_value = "[CH3:1][OH:2]"
        result = bridge_mod.execute_operation(ctx, "get_mapped_smiles", {})
    rd.chem.Mol.assert_called_once_with(mol)  # works on a copy, editor mol untouched
    atom0.SetAtomMapNum.assert_called_once_with(1)
    atom1.SetAtomMapNum.assert_called_once_with(2)
    assert result["loaded"] is True
    assert result["mapped_smiles"] == "[CH3:1][OH:2]"
    assert result["atoms"] == [
        {"index": 0, "map_num": 1, "symbol": "C"},
        {"index": 1, "map_num": 2, "symbol": "O"},
    ]


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


def test_execute_trigger_3d_conversion_no_molecule_raises(bridge_mod, ctx):
    mw = MagicMock(spec=[])  # no compute_manager
    ctx.get_main_window.return_value = mw
    ctx.current_molecule = None
    with pytest.raises(ValueError, match="No molecule loaded"):
        bridge_mod.execute_operation(ctx, "trigger_3d_conversion", {})


def test_execute_trigger_3d_conversion_embed_failure_raises(bridge_mod, ctx):
    mw = MagicMock(spec=[])  # no compute_manager → fallback path
    ctx.get_main_window.return_value = mw
    mol_mock = MagicMock()
    ctx.current_molecule = mol_mock

    allchem_mock = MagicMock(name="AllChem")
    allchem_mock.EmbedMolecule.return_value = 1  # non-zero = failure
    allchem_mock.ETKDGv3.return_value = MagicMock()

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
        with pytest.raises(ValueError, match="3D embedding failed"):
            bridge_mod.execute_operation(ctx, "trigger_3d_conversion", {})
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


# ---------------------------------------------------------------------------
# _find_moleditpy_spec / list_app_source_tree / get_app_source
# ---------------------------------------------------------------------------


def test_find_moleditpy_spec_not_found_raises(bridge_mod):
    import importlib.util

    original_find_spec = importlib.util.find_spec
    importlib.util.find_spec = lambda name: None
    try:
        with pytest.raises(ValueError, match="not found"):
            bridge_mod._find_moleditpy_spec()
    finally:
        importlib.util.find_spec = original_find_spec


def test_find_moleditpy_spec_found(bridge_mod, tmp_path):
    import importlib.util

    fake_spec = MagicMock()
    fake_spec.submodule_search_locations = [str(tmp_path)]
    original_find_spec = importlib.util.find_spec
    importlib.util.find_spec = lambda name: fake_spec if name == "moleditpy" else None
    try:
        assert bridge_mod._find_moleditpy_spec() is fake_spec
    finally:
        importlib.util.find_spec = original_find_spec


def test_execute_list_app_source_tree_spec_missing_locations_raises(bridge_mod, ctx):
    fake_spec = MagicMock()
    fake_spec.submodule_search_locations = []
    original = bridge_mod._find_moleditpy_spec
    bridge_mod._find_moleditpy_spec = lambda: fake_spec
    try:
        with pytest.raises(ValueError, match="not found"):
            bridge_mod.execute_operation(ctx, "list_app_source_tree", {})
    finally:
        bridge_mod._find_moleditpy_spec = original


def test_execute_list_app_source_tree_path_outside_package_raises(bridge_mod, ctx, tmp_path):
    fake_spec = MagicMock()
    fake_spec.submodule_search_locations = [str(tmp_path)]
    original = bridge_mod._find_moleditpy_spec
    bridge_mod._find_moleditpy_spec = lambda: fake_spec
    try:
        with pytest.raises(ValueError, match="outside the moleditpy package"):
            bridge_mod.execute_operation(ctx, "list_app_source_tree", {"path": "../../etc"})
    finally:
        bridge_mod._find_moleditpy_spec = original


def test_execute_get_app_source_directory(bridge_mod, ctx, tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "a.py").write_text("x = 1", encoding="utf-8")
    fake_spec = MagicMock()
    fake_spec.submodule_search_locations = [str(tmp_path)]
    original = bridge_mod._find_moleditpy_spec
    bridge_mod._find_moleditpy_spec = lambda: fake_spec
    try:
        result = bridge_mod.execute_operation(ctx, "get_app_source", {"path": "core"})
    finally:
        bridge_mod._find_moleditpy_spec = original
    assert result["type"] == "directory"
    assert "a.py" in result["content"]


def test_execute_get_app_source_file(bridge_mod, ctx, tmp_path):
    (tmp_path / "mod.py").write_text("print('hi')", encoding="utf-8")
    fake_spec = MagicMock()
    fake_spec.submodule_search_locations = [str(tmp_path)]
    original = bridge_mod._find_moleditpy_spec
    bridge_mod._find_moleditpy_spec = lambda: fake_spec
    try:
        result = bridge_mod.execute_operation(ctx, "get_app_source", {"path": "mod.py"})
    finally:
        bridge_mod._find_moleditpy_spec = original
    assert result["type"] == "file"
    assert "print" in result["content"]


def test_execute_get_app_source_not_exists_raises(bridge_mod, ctx, tmp_path):
    fake_spec = MagicMock()
    fake_spec.submodule_search_locations = [str(tmp_path)]
    original = bridge_mod._find_moleditpy_spec
    bridge_mod._find_moleditpy_spec = lambda: fake_spec
    try:
        with pytest.raises(ValueError, match="does not exist"):
            bridge_mod.execute_operation(ctx, "get_app_source", {"path": "nope.py"})
    finally:
        bridge_mod._find_moleditpy_spec = original


def test_execute_get_app_source_too_large_raises(bridge_mod, ctx, tmp_path):
    (tmp_path / "big.py").write_bytes(b"x" * (201 * 1024))
    fake_spec = MagicMock()
    fake_spec.submodule_search_locations = [str(tmp_path)]
    original = bridge_mod._find_moleditpy_spec
    bridge_mod._find_moleditpy_spec = lambda: fake_spec
    try:
        with pytest.raises(ValueError, match="exceeds"):
            bridge_mod.execute_operation(ctx, "get_app_source", {"path": "big.py"})
    finally:
        bridge_mod._find_moleditpy_spec = original


def test_execute_get_app_source_outside_package_raises(bridge_mod, ctx, tmp_path):
    fake_spec = MagicMock()
    fake_spec.submodule_search_locations = [str(tmp_path)]
    original = bridge_mod._find_moleditpy_spec
    bridge_mod._find_moleditpy_spec = lambda: fake_spec
    try:
        with pytest.raises(ValueError, match="outside the moleditpy package"):
            bridge_mod.execute_operation(ctx, "get_app_source", {"path": "../etc/passwd"})
    finally:
        bridge_mod._find_moleditpy_spec = original


def test_execute_get_app_source_spec_missing_locations_raises(bridge_mod, ctx):
    fake_spec = MagicMock()
    fake_spec.submodule_search_locations = []
    original = bridge_mod._find_moleditpy_spec
    bridge_mod._find_moleditpy_spec = lambda: fake_spec
    try:
        with pytest.raises(ValueError, match="not found"):
            bridge_mod.execute_operation(ctx, "get_app_source", {"path": "x.py"})
    finally:
        bridge_mod._find_moleditpy_spec = original


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


def test_execute_highlight_atoms_no_controller_raises(bridge_mod, ctx):
    ctx.get_3d_controller.return_value = None
    with pytest.raises(ValueError, match="3D controller is not available"):
        bridge_mod.execute_operation(
            ctx, "highlight_atoms", {"atom_colors": {"0": "#FF0000"}}
        )


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
    assert result["bonds_colored"] == 2


def test_execute_highlight_bonds_empty_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="Provide"):
        bridge_mod.execute_operation(ctx, "highlight_bonds", {"bond_colors": {}})


def test_execute_bond_colors_by_atom_pair(bridge_mod, ctx):
    mol = MagicMock()
    bond = MagicMock()
    bond.GetIdx.return_value = 7
    mol.GetBondBetweenAtoms.return_value = bond
    ctx.current_molecule = mol
    result = bridge_mod.execute_operation(
        ctx, "highlight_bonds", {"atom_pair_colors": {"0-3": "#00FF00"}}
    )
    mol.GetBondBetweenAtoms.assert_called_once_with(0, 3)
    ctx.get_3d_controller.return_value.set_bond_color.assert_called_once_with(7, "#00FF00")
    assert result["bonds_colored"] == 1


def test_execute_bond_colors_atom_pair_no_bond_raises(bridge_mod, ctx):
    mol = MagicMock()
    mol.GetBondBetweenAtoms.return_value = None
    ctx.current_molecule = mol
    with pytest.raises(ValueError, match="No bond exists between atoms 0 and 5"):
        bridge_mod.execute_operation(
            ctx, "highlight_bonds", {"atom_pair_colors": {"0-5": "#00FF00"}}
        )


def test_execute_bond_colors_bad_pair_key_raises(bridge_mod, ctx):
    ctx.current_molecule = MagicMock()
    with pytest.raises(ValueError, match="Invalid atom pair"):
        bridge_mod.execute_operation(
            ctx, "highlight_bonds", {"atom_pair_colors": {"nonsense": "#00FF00"}}
        )


def test_execute_bond_colors_pair_non_int_raises(bridge_mod, ctx):
    """A pair with a separator but non-integer parts must also raise
    'Invalid atom pair' (exercises the except-ValueError-then-break path)."""
    ctx.current_molecule = MagicMock()
    with pytest.raises(ValueError, match="Invalid atom pair"):
        bridge_mod.execute_operation(
            ctx, "highlight_bonds", {"atom_pair_colors": {"a-b": "#00FF00"}}
        )


def test_execute_bond_colors_pair_no_molecule_raises(bridge_mod, ctx):
    ctx.current_molecule = None
    with pytest.raises(ValueError, match="No molecule with 3D data"):
        bridge_mod.execute_operation(
            ctx, "highlight_bonds", {"atom_pair_colors": {"0-3": "#00FF00"}}
        )


def test_execute_highlight_bonds_no_controller_raises(bridge_mod, ctx):
    ctx.get_3d_controller.return_value = None
    with pytest.raises(ValueError, match="3D controller is not available"):
        bridge_mod.execute_operation(
            ctx, "highlight_bonds", {"bond_colors": {"0": "#FF0000"}}
        )


# ---------------------------------------------------------------------------
# push_undo_checkpoint / enter_3d_mode / fit_2d_view / reset_3d_camera
# ---------------------------------------------------------------------------


def test_execute_push_undo_checkpoint(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "push_undo_checkpoint", {})
    ctx.push_undo_checkpoint.assert_called_once()
    assert result["success"] is True


def test_execute_enter_3d_mode(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "enter_3d_mode", {})
    ctx.enter_3d_viewer_mode.assert_called_once()
    assert result["success"] is True


def test_execute_exit_3d_mode_via_context(bridge_mod, ctx):
    """Uses ctx.exit_3d_viewer_mode when the context provides it."""
    result = bridge_mod.execute_operation(ctx, "exit_3d_mode", {})
    ctx.exit_3d_viewer_mode.assert_called_once()
    assert result["success"] is True


def test_execute_exit_3d_mode_ui_manager_fallback(bridge_mod, ctx):
    """Falls back to mw.ui_manager.restore_ui_for_editing on older contexts."""
    del ctx.exit_3d_viewer_mode
    mw = ctx.get_main_window.return_value
    result = bridge_mod.execute_operation(ctx, "exit_3d_mode", {})
    mw.ui_manager.restore_ui_for_editing.assert_called_once()
    assert result["success"] is True


def test_execute_exit_3d_mode_no_main_window(bridge_mod, ctx):
    del ctx.exit_3d_viewer_mode
    ctx.get_main_window.return_value = None
    with pytest.raises(ValueError, match="not available"):
        bridge_mod.execute_operation(ctx, "exit_3d_mode", {})


def test_execute_exit_3d_mode_unsupported_app(bridge_mod, ctx):
    del ctx.exit_3d_viewer_mode
    mw = ctx.get_main_window.return_value
    del mw.ui_manager.restore_ui_for_editing
    with pytest.raises(ValueError, match="does not support"):
        bridge_mod.execute_operation(ctx, "exit_3d_mode", {})


def test_execute_fit_2d_view(bridge_mod, ctx):
    result = bridge_mod.execute_operation(ctx, "fit_2d_view", {})
    ctx.fit_2d_view.assert_called_once()
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
# get_plugin_dir / reload_plugins
# ---------------------------------------------------------------------------


def test_execute_get_plugin_dir(bridge_mod, ctx):
    mw = ctx.get_main_window.return_value
    mw.plugin_manager.plugin_dir = "/home/user/.moleditpy/plugins"
    result = bridge_mod.execute_operation(ctx, "get_plugin_dir", {})
    assert "/home/user/.moleditpy/plugins" in result["plugin_dir"]


def test_execute_get_plugin_dir_no_plugin_manager(bridge_mod, ctx):
    mw_mock = MagicMock(spec=[])  # no attributes → hasattr returns False
    ctx.get_main_window.return_value = mw_mock
    with pytest.raises(ValueError, match="not available"):
        bridge_mod.execute_operation(ctx, "get_plugin_dir", {})


def test_execute_reload_plugins(bridge_mod, ctx):
    mw = ctx.get_main_window.return_value
    mw.plugin_manager.discover_plugins.return_value = [MagicMock(), MagicMock()]
    result = bridge_mod.execute_operation(ctx, "reload_plugins", {})
    assert result["success"] is True
    assert result["plugin_count"] == 2
    mw.plugin_manager.discover_plugins.assert_called_once_with(mw)


def test_execute_reload_plugins_returns_none(bridge_mod, ctx):
    mw = ctx.get_main_window.return_value
    mw.plugin_manager.discover_plugins.return_value = None
    result = bridge_mod.execute_operation(ctx, "reload_plugins", {})
    assert result["plugin_count"] == 0


def test_execute_reload_plugins_no_plugin_manager_raises(bridge_mod, ctx):
    mw_mock = MagicMock(spec=[])  # no attributes → hasattr returns False
    ctx.get_main_window.return_value = mw_mock
    with pytest.raises(ValueError, match="not available"):
        bridge_mod.execute_operation(ctx, "reload_plugins", {})


# ---------------------------------------------------------------------------
# get_app_source
# ---------------------------------------------------------------------------


def test_execute_get_app_source_missing_path_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="required"):
        bridge_mod.execute_operation(ctx, "get_app_source", {"path": ""})


# ---------------------------------------------------------------------------
# list_app_source_tree
# ---------------------------------------------------------------------------


def test_execute_list_app_source_tree(bridge_mod, ctx, tmp_path):
    # Build a fake package in tmp_path
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "molecular_data.py").write_text("# data", encoding="utf-8")
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "plugin_interface.py").write_text("# interface", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cache.pyc").write_text("junk", encoding="utf-8")

    fake_spec = MagicMock()
    fake_spec.submodule_search_locations = [str(tmp_path)]

    # Patch _find_moleditpy_spec directly on the isolated module object so
    # _list_app_source_tree picks it up from its own __globals__.
    original = bridge_mod._find_moleditpy_spec
    bridge_mod._find_moleditpy_spec = lambda: fake_spec
    try:
        result = bridge_mod.execute_operation(ctx, "list_app_source_tree", {})
    finally:
        bridge_mod._find_moleditpy_spec = original

    tree = result["content"]
    assert "core" in tree
    assert "plugins" in tree
    assert "molecular_data.py" in tree
    assert "plugin_interface.py" in tree
    assert "__pycache__" not in tree


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


def test_app_version_from_init_manager_settings(bridge_mod, ctx):
    mw = ctx.get_main_window.return_value
    mw.VERSION = None
    mw.init_manager.settings = {"app_version": "4.4.2"}
    result = bridge_mod.execute_operation(ctx, "get_app_info", {})
    assert result["version"] == "4.4.2"


def test_app_version_falls_back_to_moleditpy_package(bridge_mod, ctx):
    """The main window exposes no VERSION attribute — the package is the real source."""
    mw = ctx.get_main_window.return_value
    mw.VERSION = None
    mw.init_manager.settings = {}
    fake = types.ModuleType("moleditpy")
    fake.__version__ = "4.5.0"
    saved = sys.modules.get("moleditpy")
    sys.modules["moleditpy"] = fake
    try:
        result = bridge_mod.execute_operation(ctx, "get_app_info", {})
    finally:
        if saved is None:
            sys.modules.pop("moleditpy", None)
        else:
            sys.modules["moleditpy"] = saved

    assert result["version"] == "4.5.0"


def test_app_version_unknown_without_main_window(bridge_mod, ctx):
    ctx.get_main_window.return_value = None
    saved = sys.modules.get("moleditpy")
    sys.modules["moleditpy"] = None  # a None entry makes the import raise ImportError
    try:
        result = bridge_mod.execute_operation(ctx, "get_app_info", {})
    finally:
        if saved is None:
            sys.modules.pop("moleditpy", None)
        else:
            sys.modules["moleditpy"] = saved

    assert result["version"] == "unknown"


def test_plugin_version_uses_own_package(bridge_mod):
    """A subfolder install (AI.mcp_server) has no importable top-level mcp_server."""
    pkg = types.ModuleType("AI.mcp_server")
    pkg.__path__ = []
    pkg.PLUGIN_VERSION = "9.9.9"
    saved_pkg = bridge_mod.__package__
    saved_name = bridge_mod.__spec__.name  # spec.parent derives from spec.name
    saved_abs = sys.modules.pop("mcp_server", None)
    sys.modules["AI.mcp_server"] = pkg
    bridge_mod.__package__ = "AI.mcp_server"
    bridge_mod.__spec__.name = "AI.mcp_server.bridge"
    try:
        assert bridge_mod._plugin_version() == "9.9.9"
    finally:
        bridge_mod.__package__ = saved_pkg
        bridge_mod.__spec__.name = saved_name
        sys.modules.pop("AI.mcp_server", None)
        if saved_abs is not None:
            sys.modules["mcp_server"] = saved_abs


def test_plugin_version_unknown_when_unresolvable(bridge_mod):
    saved_abs = sys.modules.pop("mcp_server", None)
    saved_path = list(sys.path)
    sys.path[:] = []
    try:
        assert bridge_mod._plugin_version() == "unknown"
    finally:
        sys.path[:] = saved_path
        if saved_abs is not None:
            sys.modules["mcp_server"] = saved_abs


def test_get_app_info_survives_missing_mcp_server_module(bridge_mod, ctx):
    """Regression: _get_app_info raised ModuleNotFoundError on subfolder installs."""
    ctx.get_main_window.return_value.VERSION = "4.5.0"
    saved_abs = sys.modules.pop("mcp_server", None)
    saved_path = list(sys.path)
    sys.path[:] = []
    try:
        result = bridge_mod.execute_operation(ctx, "get_app_info", {})
    finally:
        sys.path[:] = saved_path
        if saved_abs is not None:
            sys.modules["mcp_server"] = saved_abs

    assert result["version"] == "4.5.0"
    assert result["mcp_plugin_version"] == "unknown"


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


# ---------------------------------------------------------------------------
# open_plugin_installer
# ---------------------------------------------------------------------------


def _menu_action(text, submenu=None):
    action = MagicMock()
    action.text.return_value = text
    action.menu.return_value = submenu
    return action


def test_open_plugin_installer_found(bridge_mod, ctx):
    installer = _menu_action("Plugin Installer...")
    submenu = MagicMock()
    submenu.actions.return_value = [_menu_action("Reload Plugins"), installer]
    plugin_menu = _menu_action("&Plugin", submenu=submenu)
    ctx.get_main_window.return_value.menuBar.return_value.actions.return_value = [
        _menu_action("&File"), plugin_menu,
    ]

    result = bridge_mod.execute_operation(ctx, "open_plugin_installer", {})
    assert result["found"] is True
    bridge_mod.QTimer.singleShot.assert_called_once_with(0, installer.trigger)


def test_open_plugin_installer_not_found(bridge_mod, ctx):
    ctx.get_main_window.return_value.menuBar.return_value.actions.return_value = [
        _menu_action("&File"), _menu_action("&Edit"),
    ]
    result = bridge_mod.execute_operation(ctx, "open_plugin_installer", {})
    assert result["found"] is False


def test_open_plugin_installer_no_mw_raises(bridge_mod, ctx):
    ctx.get_main_window.return_value = None
    with pytest.raises(ValueError, match="Main window"):
        bridge_mod.execute_operation(ctx, "open_plugin_installer", {})


# ---------------------------------------------------------------------------
# reset_cpk_color_override
# ---------------------------------------------------------------------------


def _v3d_with_overrides(ctx, atoms=None, bonds=None):
    v3d = MagicMock()
    v3d._plugin_color_overrides = dict(atoms or {})
    v3d._plugin_bond_color_overrides = dict(bonds or {})
    ctx.get_main_window.return_value.view_3d_manager = v3d
    return v3d


def test_reset_cpk_override_all(bridge_mod, ctx):
    v3d = _v3d_with_overrides(ctx, atoms={0: "#FF0000", 2: "#00FF00"}, bonds={1: "#0000FF"})
    result = bridge_mod.execute_operation(ctx, "reset_cpk_color_override", {})
    assert result == {"cleared_atoms": 2, "cleared_bonds": 1}
    assert v3d._plugin_color_overrides == {}
    assert v3d._plugin_bond_color_overrides == {}
    v3d.draw_molecule_3d.assert_called_once_with(v3d.current_mol)


def test_reset_cpk_override_atoms_only(bridge_mod, ctx):
    v3d = _v3d_with_overrides(ctx, atoms={0: "#FF0000"}, bonds={1: "#0000FF"})
    result = bridge_mod.execute_operation(ctx, "reset_cpk_color_override", {"scope": "atoms"})
    assert result == {"cleared_atoms": 1, "cleared_bonds": 0}
    assert v3d._plugin_bond_color_overrides == {1: "#0000FF"}


def test_reset_cpk_override_nothing_to_clear_skips_redraw(bridge_mod, ctx):
    v3d = _v3d_with_overrides(ctx)
    result = bridge_mod.execute_operation(ctx, "reset_cpk_color_override", {})
    assert result == {"cleared_atoms": 0, "cleared_bonds": 0}
    v3d.draw_molecule_3d.assert_not_called()


def test_reset_cpk_override_bad_scope_raises(bridge_mod, ctx):
    with pytest.raises(ValueError, match="scope"):
        bridge_mod.execute_operation(ctx, "reset_cpk_color_override", {"scope": "everything"})


def test_reset_cpk_override_no_v3d_raises(bridge_mod, ctx):
    mw = MagicMock(spec=[])  # no view_3d_manager attribute
    ctx.get_main_window.return_value = mw
    with pytest.raises(ValueError, match="3D view"):
        bridge_mod.execute_operation(ctx, "reset_cpk_color_override", {})


def test_execute_get_app_source_root(bridge_mod, ctx, tmp_path):
    fake_spec = MagicMock()
    fake_spec.submodule_search_locations = [str(tmp_path)]
    original = bridge_mod._find_moleditpy_spec
    bridge_mod._find_moleditpy_spec = lambda: fake_spec
    try:
        result = bridge_mod.execute_operation(ctx, "get_app_source_root", {})
    finally:
        bridge_mod._find_moleditpy_spec = original
    assert Path(result["root"]) == tmp_path.resolve()


def test_get_app_source_root_without_package_raises(bridge_mod):
    original = bridge_mod._find_moleditpy_spec
    bridge_mod._find_moleditpy_spec = lambda: None
    try:
        with pytest.raises(ValueError, match="not found"):
            bridge_mod._get_app_source_root()
    finally:
        bridge_mod._find_moleditpy_spec = original


# ---------------------------------------------------------------------------
# apply_reaction_smarts with REAL RDKit.
#
# The rest of this file mocks RDKit, which cannot express the bug these cover:
# AddHs makes hydrogens real atoms, so a product that lowers a mapped atom's
# hydrogen count is over-valent and fails sanitization. The reactant fallback
# has to trigger on an invalid product, not only on "no match".
# ---------------------------------------------------------------------------


def _real_bridge():
    """The bridge module with real RDKit (its rdkit imports are lazy)."""
    import importlib.util
    from pathlib import Path

    pytest.importorskip("rdkit")
    path = Path(__file__).resolve().parents[1] / "mcp_server" / "bridge.py"
    spec = importlib.util.spec_from_file_location("_bridge_real_rdkit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(smarts, smiles):
    # importorskip BEFORE the rdkit import, or a missing RDKit fails the
    # test instead of skipping it.
    pytest.importorskip("rdkit")
    from rdkit import Chem

    mod = _real_bridge()
    ctx = MagicMock()
    ctx.current_molecule = Chem.MolFromSmiles(smiles)
    return mod._apply_reaction_smarts(
        ctx, {"reaction_smarts": smarts, "convert_to_3d": False}
    )


@pytest.mark.parametrize(
    "label,smiles,smarts,expected",
    [
        ("alcohol to aldehyde", "CCO", "[CH2:1][OH:2]>>[CH:1]=[O:2]", "CC=O"),
        ("longer chain", "CCCO", "[CH2:1][OH:2]>>[CH:1]=[O:2]", "CCC=O"),
        ("alcohol to ketone", "CC(O)C", "[CH:1][OH:2]>>[C:1]=[O:2]", "CC(C)=O"),
        ("amine to imine", "CCN", "[CH2:1][NH2:2]>>[CH:1]=[N:2]", "CC=N"),
    ],
)
def test_hydrogen_lowering_smarts_applies(label, smiles, smarts, expected):
    """These all failed with "refine the SMARTS" while the SMARTS was correct."""
    assert _apply(smarts, smiles)["smiles"] == expected


def test_explicit_h_smarts_still_needs_the_addhs_attempt():
    """The AddHs attempt must stay first: this pattern matches [H] directly."""
    assert _apply("[cH:1][H]>>[c:1]O", "c1ccccc1")["smiles"] == "Oc1ccccc1"


def test_unmatched_pattern_still_reports_no_match():
    with pytest.raises(ValueError, match="did not match"):
        _apply("[N:1]>>[O:1]", "CCO")


def test_genuinely_invalid_product_still_reports_sanitization():
    with pytest.raises(ValueError, match="invalid molecule"):
        _apply("[C:1][O:2]>>[C:1][O:2]([H])([H])([H])", "CCO")

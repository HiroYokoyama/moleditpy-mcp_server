#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Thread-safe bridge between the MCP HTTP server thread and the Qt main thread.

The MCP server runs in a background thread and cannot call MoleditPy's
PluginContext methods directly (Qt requires all UI operations on the main thread).
MCPBridge solves this by emitting a queued Qt signal from the server thread;
Qt automatically delivers it to the main thread's event loop, which runs the
operation and sets a threading.Event to wake the waiting server thread.

The pure-Python dispatch logic lives in ``execute_operation`` (a module-level
function with no Qt dependency) so it can be unit-tested without a QApplication.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal

logger = logging.getLogger(__name__)

# Default set of extensions the file I/O tools are allowed to touch.
# Covers common DFT/QM input formats, plain text, and data files.
_DEFAULT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".inp", ".gjf", ".com", ".nw", ".in", ".orca",
        ".xyz", ".mol", ".mol2", ".sdf", ".pdb", ".cif",
        ".txt", ".csv", ".dat", ".log", ".out",
        ".json", ".yaml", ".yml",
        ".py", ".sh", ".bash",
        ".fchk", ".chk", ".cfg", ".conf",
    }
)


# ---------------------------------------------------------------------------
# Pure-Python dispatch — no Qt dependency, fully unit-testable
# ---------------------------------------------------------------------------


def execute_operation(ctx: Any, operation: str, args: Dict[str, Any]) -> Any:  # noqa: C901
    """
    Dispatch *operation* to the appropriate PluginContext method and return
    the result. All code here runs on the Qt main thread (via MCPBridge).

    Args:
        ctx:       The active ``PluginContext`` instance.
        operation: One of the named operations understood by the bridge.
        args:      Keyword arguments for the operation (may be empty).

    Raises:
        ValueError: If *operation* is unrecognised.
    """
    if operation == "get_molecule_info":
        return _get_molecule_info(ctx)

    if operation == "get_xyz_block":
        xyz = ctx.to_xyz_block()
        return {"xyz_block": xyz, "has_data": xyz is not None}

    if operation == "load_smiles":
        smiles = args.get("smiles", "").strip()
        if not smiles:
            raise ValueError("'smiles' argument is required")
        ctx.load_from_smiles(smiles)
        return {"success": True}

    if operation == "show_xyz":
        xyz_text = args.get("xyz_text", "").strip()
        source_name = args.get("source_name", "MCP input")
        if not xyz_text:
            raise ValueError("'xyz_text' argument is required")
        mol = ctx.show_xyz_data(xyz_text, source_name=source_name)
        return {"success": mol is not None}

    if operation == "get_atom_properties":
        return _get_atom_properties(ctx, args.get("atom_indices") or [])

    if operation == "get_xyz_atoms":
        return _get_xyz_atoms(ctx)

    if operation == "get_bond_info":
        return _get_bond_info(ctx)

    if operation == "load_mol_block":
        return _load_mol_block(ctx, args)

    if operation == "apply_reaction_smarts":
        return _apply_reaction_smarts(ctx, args)

    if operation == "get_mapped_smiles":
        return _get_mapped_smiles(ctx)

    if operation == "trigger_3d_conversion":
        return _trigger_3d_conversion(ctx)

    if operation == "highlight_atoms":
        atom_colors = args.get("atom_colors")
        if not atom_colors:
            raise ValueError("'atom_colors' argument is required")
        ctrl = ctx.get_3d_controller()
        if ctrl is None:
            raise ValueError("3D controller is not available (is the 3D viewer active?)")
        for idx_str, color in atom_colors.items():
            ctrl.set_atom_color(int(idx_str), color)
        ctx.refresh_3d_view()
        return {"success": True}

    if operation == "highlight_bonds":
        return _set_bond_colors(ctx, args)

    if operation == "push_undo_checkpoint":
        ctx.push_undo_checkpoint()
        return {"success": True}

    if operation == "enter_3d_mode":
        ctx.enter_3d_viewer_mode()
        return {"success": True}

    if operation == "exit_3d_mode":
        return _exit_3d_mode(ctx)

    if operation == "fit_2d_view":
        ctx.fit_2d_view()
        return {"success": True}

    if operation == "reset_3d_camera":
        ctx.reset_3d_camera()
        return {"success": True}

    if operation == "refresh_3d_view":
        ctx.refresh_3d_view()
        return {"success": True}

    if operation == "check_chemistry":
        ctx.check_chemistry_problems()
        ctx.refresh_ui()
        return {"success": True}

    if operation == "refresh_ui":
        ctx.refresh_ui()
        return {"success": True}

    if operation == "run_python":
        return _run_python(ctx, args)

    if operation == "get_selected_atoms":
        return _get_selected_atoms(ctx)

    if operation == "clear_canvas":
        ctx.clear_canvas(push_to_undo=True)
        return {"success": True}

    if operation == "get_app_info":
        return _get_app_info(ctx)

    if operation == "get_plugin_dir":
        mw = ctx.get_main_window()
        if mw is None or not hasattr(mw, "plugin_manager"):
            raise ValueError("Plugin manager is not available on main window")
        return {"plugin_dir": str(mw.plugin_manager.plugin_dir)}

    if operation == "reload_plugins":
        mw = ctx.get_main_window()
        if mw is None or not hasattr(mw, "plugin_manager"):
            raise ValueError("Plugin manager is not available on main window")
        plugins = mw.plugin_manager.discover_plugins(mw)
        return {"success": True, "plugin_count": len(plugins) if plugins else 0}

    if operation == "list_app_source_tree":
        return _list_app_source_tree(args)

    if operation == "get_app_source":
        return _get_app_source(args)

    if operation == "get_app_source_root":
        return _get_app_source_root()

    if operation == "reset_cpk_color_override":
        return _reset_cpk_color_override(ctx, args)

    if operation == "open_plugin_installer":
        return _open_plugin_installer(ctx)

    if operation == "get_file_io_config":
        return _get_file_io_config(ctx)

    if operation == "set_file_io_config":
        return _set_file_io_config(ctx, args)

    if operation == "get_molecule_image":
        return _get_molecule_image(ctx, args)

    if operation == "get_molecule_descriptors":
        return _get_molecule_descriptors(ctx)

    if operation == "add_hydrogens":
        return _add_hydrogens(ctx, args)

    if operation == "remove_hydrogens":
        return _remove_hydrogens(ctx)

    if operation == "optimize_geometry":
        return _optimize_geometry(ctx, args)

    if operation == "set_atom_charge":
        return _set_atom_charge(ctx, args)

    if operation == "delete_atoms":
        return _delete_atoms(ctx, args)

    if operation == "substructure_search":
        return _substructure_search(ctx, args)

    if operation == "compute_partial_charges":
        return _compute_partial_charges(ctx, args)

    raise ValueError(f"Unknown operation: {operation!r}")


def _parse_atom_pair(pair: str) -> tuple:
    """Parse an 'i-j' (or 'i,j') atom-pair key into two int indices."""
    for sep in ("-", ","):
        if sep in pair:
            left, _, right = pair.partition(sep)
            try:
                return int(left.strip()), int(right.strip())
            except ValueError:
                break
    raise ValueError(
        f"Invalid atom pair {pair!r}: expected 'atomIndex1-atomIndex2', e.g. '0-3'."
    )


def _set_bond_colors(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    bond_colors = args.get("bond_colors") or {}
    pair_colors = args.get("atom_pair_colors") or {}
    if not bond_colors and not pair_colors:
        raise ValueError("Provide 'bond_colors' and/or 'atom_pair_colors'.")
    ctrl = ctx.get_3d_controller()
    if ctrl is None:
        raise ValueError("3D controller is not available (is the 3D viewer active?)")

    resolved: Dict[int, str] = {}
    if pair_colors:
        mol = ctx.current_molecule
        if mol is None:
            raise ValueError("No molecule with 3D data — run trigger_3d_conversion first.")
        for pair, color in pair_colors.items():
            idx1, idx2 = _parse_atom_pair(str(pair))
            bond = mol.GetBondBetweenAtoms(idx1, idx2)
            if bond is None:
                raise ValueError(f"No bond exists between atoms {idx1} and {idx2}.")
            resolved[bond.GetIdx()] = color
    for idx_str, color in bond_colors.items():
        resolved[int(idx_str)] = color

    for bond_idx, color in resolved.items():
        ctrl.set_bond_color(bond_idx, color)
    ctx.refresh_3d_view()
    return {"success": True, "bonds_colored": len(resolved)}


def _reset_cpk_color_override(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Clear plugin CPK color overrides (atoms and/or bonds) and redraw once."""
    scope = args.get("scope", "all")
    if scope not in ("atoms", "bonds", "all"):
        raise ValueError("'scope' must be 'atoms', 'bonds', or 'all'")
    mw = ctx.get_main_window()
    v3d = getattr(mw, "view_3d_manager", None) if mw else None
    if v3d is None:
        raise ValueError("3D view is not available")
    # Overrides are stored on the 3D manager and reapplied on every redraw;
    # update_*_color_override(idx, None) removes one entry but triggers a
    # full redraw per call, so clear the stores directly and redraw once.
    cleared_atoms = cleared_bonds = 0
    if scope in ("atoms", "all") and hasattr(v3d, "_plugin_color_overrides"):
        cleared_atoms = len(v3d._plugin_color_overrides)
        v3d._plugin_color_overrides.clear()
    if scope in ("bonds", "all") and hasattr(v3d, "_plugin_bond_color_overrides"):
        cleared_bonds = len(v3d._plugin_bond_color_overrides)
        v3d._plugin_bond_color_overrides.clear()
    if getattr(v3d, "current_mol", None) is not None and (cleared_atoms or cleared_bonds):
        v3d.draw_molecule_3d(v3d.current_mol)
    return {"cleared_atoms": cleared_atoms, "cleared_bonds": cleared_bonds}


def _find_menu_action(actions: Any, needle: str) -> Any:
    """Recursively search QAction lists (incl. submenus) for a text match."""
    for action in actions:
        text = str(action.text()).replace("&", "").replace("...", "").strip()
        if needle.lower() in text.lower():
            return action
        submenu = action.menu()
        if submenu is not None:
            found = _find_menu_action(submenu.actions(), needle)
            if found is not None:
                return found
    return None


def _open_plugin_installer(ctx: Any) -> Dict[str, Any]:
    mw = ctx.get_main_window()
    if mw is None:
        raise ValueError("Main window is not available")
    action = _find_menu_action(mw.menuBar().actions(), "Plugin Installer")
    if action is None:
        return {"found": False}
    # The installer opens a modal dialog (dlg.exec()); trigger it only after
    # this bridge call has returned so the MCP request does not block inside
    # the dialog's event loop.
    QTimer.singleShot(0, action.trigger)
    return {"found": True}


def _get_molecule_info(ctx: Any) -> Dict[str, Any]:
    mol = ctx.current_molecule
    if mol is None:
        return {
            "loaded": False,
            "smiles": None,
            "formula": None,
            "molecular_weight": 0.0,
            "num_atoms": 0,
            "num_bonds": 0,
            "has_3d_coords": False,
        }
    from rdkit import Chem  # pylint: disable=import-outside-toplevel
    from rdkit.Chem import Descriptors, rdMolDescriptors  # pylint: disable=import-outside-toplevel
    return {
        "loaded": True,
        "smiles": Chem.MolToSmiles(mol),
        "formula": rdMolDescriptors.CalcMolFormula(mol),
        "molecular_weight": round(Descriptors.MolWt(mol), 4),
        "num_atoms": mol.GetNumAtoms(),
        "num_bonds": mol.GetNumBonds(),
        "has_3d_coords": mol.GetNumConformers() > 0,
    }


def _get_atom_properties(ctx: Any, atom_indices: List[int]) -> Dict[str, Any]:
    mol = ctx.current_molecule
    if mol is None:
        return {"atoms": []}
    if not atom_indices:
        atom_indices = list(range(mol.GetNumAtoms()))
    atoms: List[Dict[str, Any]] = []
    for idx in atom_indices:
        atom = mol.GetAtomWithIdx(idx)
        atoms.append(
            {
                "index": idx,
                "symbol": atom.GetSymbol(),
                "atomic_num": atom.GetAtomicNum(),
                "formal_charge": atom.GetFormalCharge(),
                "hybridization": str(atom.GetHybridization()),
                "total_hs": atom.GetTotalNumHs(),
                "num_radical_electrons": atom.GetNumRadicalElectrons(),
            }
        )
    return {"atoms": atoms}


def _get_xyz_atoms(ctx: Any) -> Dict[str, Any]:
    mol = ctx.current_molecule
    if mol is None or mol.GetNumConformers() == 0:
        return {"atoms": [], "has_data": False}
    conf = mol.GetConformer()
    atoms: List[Dict[str, Any]] = []
    for atom in mol.GetAtoms():
        idx = atom.GetIdx()
        pos = conf.GetAtomPosition(idx)
        atoms.append(
            {
                "index": idx,
                "symbol": atom.GetSymbol(),
                "atomic_num": atom.GetAtomicNum(),
                "x": float(pos.x),
                "y": float(pos.y),
                "z": float(pos.z),
            }
        )
    return {"atoms": atoms, "has_data": True}


def _get_bond_info(ctx: Any) -> Dict[str, Any]:
    mol = ctx.current_molecule
    if mol is None:
        return {"bonds": []}
    _bond_type_map = {
        1.0: "SINGLE",
        2.0: "DOUBLE",
        3.0: "TRIPLE",
        1.5: "AROMATIC",
    }
    bonds: List[Dict[str, Any]] = []
    for bond in mol.GetBonds():
        bond_order = bond.GetBondTypeAsDouble()
        bonds.append(
            {
                "index": bond.GetIdx(),
                "atom1": bond.GetBeginAtomIdx(),
                "atom2": bond.GetEndAtomIdx(),
                "bond_type": _bond_type_map.get(bond_order, str(bond_order)),
            }
        )
    return {"bonds": bonds}


def _get_selected_atoms(ctx: Any) -> Dict[str, Any]:
    indices: List[int] = ctx.get_selected_atom_indices()
    mol = ctx.current_molecule
    atoms: List[Dict[str, Any]] = []
    if mol and indices:
        for idx in indices:
            atom = mol.GetAtomWithIdx(idx)
            atoms.append(
                {
                    "index": idx,
                    "symbol": atom.GetSymbol(),
                    "atomic_num": atom.GetAtomicNum(),
                }
            )
    return {"selected_atoms": atoms, "count": len(atoms)}


def _load_mol_block(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    from rdkit import Chem  # pylint: disable=import-outside-toplevel
    mol_block = args.get("mol_block", "").strip()
    if not mol_block:
        raise ValueError("'mol_block' argument is required")
    mol = Chem.MolFromMolBlock(mol_block, removeHs=False)
    if mol is None:
        return {"success": False}
    ctx.current_molecule = mol
    ctx.push_undo_checkpoint()
    ctx.refresh_ui()
    return {"success": True}


def _get_mapped_smiles(ctx: Any) -> Dict[str, Any]:
    """
    Return the current molecule's SMILES with every atom's RDKit index
    embedded as an atom map number (map number = index + 1, because RDKit
    reserves map number 0 for "unmapped"). Lets an AI client identify which
    atom_index to target in apply_reaction_smarts / highlight_atoms.
    """
    from rdkit import Chem  # pylint: disable=import-outside-toplevel
    mol = ctx.current_molecule
    if mol is None:
        return {"loaded": False, "mapped_smiles": None, "atoms": []}
    tagged = Chem.Mol(mol)
    atoms = []
    for atom in tagged.GetAtoms():
        idx = atom.GetIdx()
        atom.SetAtomMapNum(idx + 1)
        atoms.append({"index": idx, "map_num": idx + 1, "symbol": atom.GetSymbol()})
    return {
        "loaded": True,
        "mapped_smiles": Chem.MolToSmiles(tagged),
        "atoms": atoms,
    }


def _select_product_by_anchor(
    rxn: Any, reactant: Any, products: Any, atom_index: Any
) -> int:
    """
    Pick the RunReactants product whose match contains *atom_index*.

    RunReactants enumerates products in the same order as
    GetSubstructMatches(uniquify=False) enumerates matches, so the match
    index maps onto the product index. Among matches containing the anchor
    atom, the product retaining the most atoms is preferred (same heuristic
    as the Chat with Molecule plugin). Falls back to the first product.
    """
    if atom_index is None:
        return 0
    try:
        target = int(atom_index)
        matches = reactant.GetSubstructMatches(rxn.GetReactants()[0], uniquify=False)
        candidates = []
        for i, match in enumerate(matches):
            if i >= len(products):
                break
            if target in match:
                candidates.append((i, products[i][0].GetNumAtoms()))
        if candidates:
            candidates.sort(key=lambda item: item[1], reverse=True)
            return candidates[0][0]
        logger.warning(
            "Anchor atom %s not found in any reaction match; using first match", target
        )
    except Exception:  # pylint: disable=broad-except
        logger.exception("Anchor atom filtering failed; using first match")
    return 0


def _clean_reaction_product(new_mol: Any) -> Any:
    """Sanitize a raw reaction product, or return None if it is not a molecule.

    Returning None rather than raising lets the caller retry the reaction on a
    differently prepared reactant.
    """
    from rdkit import Chem  # pylint: disable=import-outside-toplevel

    try:
        new_mol.UpdatePropertyCache(strict=False)
        Chem.SanitizeMol(new_mol)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Product sanitization warning: %s", exc)
    try:
        new_mol = Chem.RemoveHs(
            new_mol, implicitOnly=False, updateExplicitCount=True, sanitize=True
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("RemoveHs failed on product: %s", exc)

    # Template atom maps leak into the product; strip them before export.
    for atom in new_mol.GetAtoms():
        atom.SetAtomMapNum(0)

    try:
        return Chem.MolFromSmiles(Chem.MolToSmiles(new_mol))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Product SMILES round-trip failed: %s", exc)
        return None


def _apply_reaction_smarts(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply a Reaction SMARTS transformation to the current molecule and load
    the product into the 2D editor.

    Adapted from the Chat with Molecule plugin's apply_transformation flow:
    run the reaction with explicit hydrogens (retry implicit), optionally
    anchor the match site to *atom_index*, guard against destructive
    products, then sanitize and round-trip through SMILES.
    """
    from rdkit import Chem  # pylint: disable=import-outside-toplevel
    from rdkit.Chem import AllChem  # pylint: disable=import-outside-toplevel

    reaction_smarts = (args.get("reaction_smarts") or "").strip()
    if not reaction_smarts:
        raise ValueError("'reaction_smarts' argument is required")

    mol = ctx.current_molecule
    if mol is None:
        raise ValueError("No molecule loaded")

    try:
        rxn = AllChem.ReactionFromSmarts(reaction_smarts)
    except Exception as exc:
        raise ValueError(f"Invalid reaction SMARTS: {exc}") from exc

    # Try with explicit hydrogens first so a SMARTS can match [H], then without.
    # Both attempts are needed: AddHs turns hydrogens into real atoms, so a
    # product that lowers a mapped atom's hydrogen count ([CH2:1] -> [CH:1]=)
    # leaves those atoms bonded and the product is over-valent. Retrying only on
    # "no match" missed that -- every oxidation-style pattern (alcohol to
    # aldehyde or ketone, amine to imine) failed with "refine the SMARTS" even
    # though the SMARTS was right.
    attempt = None
    for candidate in (Chem.AddHs(mol), mol):
        products = rxn.RunReactants((candidate,))
        if not products:
            continue
        selected = _select_product_by_anchor(
            rxn, candidate, products, args.get("atom_index")
        )
        clean_mol = _clean_reaction_product(products[selected][0])
        if clean_mol is not None:
            attempt = (products, selected, clean_mol)
            break

    if attempt is None:
        if not products:
            raise ValueError(
                "The reaction pattern did not match the current molecule. "
                "Check the SMARTS (explicit [H] atoms are available for matching)."
            )
        raise ValueError(
            "Transformation produced an invalid molecule (failed sanitization). "
            "Refine the reaction SMARTS."
        )
    products, selected, clean_mol = attempt

    # Compare heavy atoms on both sides: the editor molecule may carry
    # explicit hydrogens (e.g. after 3D conversion) while clean_mol is
    # H-stripped, and mixing the two counts falsely trips the guard.
    orig_count = mol.GetNumHeavyAtoms()
    new_count = clean_mol.GetNumHeavyAtoms()
    if orig_count > 5 and new_count < orig_count * 0.7:
        raise ValueError(
            f"Safety guard: transformation caused massive atom loss "
            f"({orig_count} -> {new_count} heavy atoms). Aborted."
        )

    final_smiles = Chem.MolToSmiles(clean_mol)
    # Atom indices are reassigned by the SMILES round-trip, so pre-apply
    # atom_index values are stale. Report the new mapping (map = index + 1)
    # computed from a re-parse of final_smiles — the same string the editor
    # is about to load — so chained transformations can target atoms
    # without an extra get_mapped_smiles call.
    mapped_smiles = None
    report_mol = Chem.MolFromSmiles(final_smiles)
    if report_mol is not None:
        for atom in report_mol.GetAtoms():
            atom.SetAtomMapNum(atom.GetIdx() + 1)
        mapped_smiles = Chem.MolToSmiles(report_mol)
    # load_from_smiles ADDS to the canvas; clear the original molecule first
    # so the product replaces it. No intermediate undo checkpoint — a single
    # undo must restore the pre-transformation state, not an empty canvas.
    ctx.clear_canvas(push_to_undo=False)
    ctx.load_from_smiles(final_smiles)
    ctx.push_undo_checkpoint()
    # ctx.current_molecule reads the 3D manager's molecule, which only the
    # 2D->3D pipeline populates. Without converting, the next
    # apply_reaction_smarts would see no molecule, breaking chained edits.
    converted_3d = False
    if args.get("convert_to_3d", True):
        try:
            _trigger_3d_conversion(ctx)
            converted_3d = True
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Post-transformation 3D conversion failed: %s", exc)
    ctx.refresh_ui()
    return {
        "success": True,
        "smiles": final_smiles,
        "num_products": len(products),
        "selected_product": selected,
        "converted_3d": converted_3d,
        "mapped_smiles": mapped_smiles,
    }


def _exit_3d_mode(ctx: Any) -> Dict[str, Any]:
    """Switch the UI back to 2D editing mode (counterpart of enter_3d_mode)."""
    if hasattr(ctx, "exit_3d_viewer_mode"):
        ctx.exit_3d_viewer_mode()
        return {"success": True}
    mw = ctx.get_main_window()
    if mw is None or not hasattr(mw, "ui_manager"):
        raise ValueError("Main window UI manager is not available")
    fn = getattr(mw.ui_manager, "restore_ui_for_editing", None)
    if fn is None:
        raise ValueError("This MoleditPy version does not support exiting 3D viewer mode")
    fn()
    return {"success": True}


def _trigger_3d_conversion(ctx: Any) -> Dict[str, Any]:
    # Prefer the native compute manager (non-blocking trigger).
    mw = ctx.get_main_window()
    if mw is not None and hasattr(mw, "compute_manager"):
        cm = mw.compute_manager
        if hasattr(cm, "trigger_conversion"):
            cm.trigger_conversion()
            return {"success": True}
    # Fallback: RDKit ETKDG + MMFF in-thread.
    from rdkit import Chem  # pylint: disable=import-outside-toplevel
    from rdkit.Chem import AllChem  # pylint: disable=import-outside-toplevel
    mol = ctx.current_molecule
    if mol is None:
        raise ValueError("No molecule loaded")
    mol_h = Chem.AddHs(mol)
    if AllChem.EmbedMolecule(mol_h, AllChem.ETKDGv3()) != 0:
        raise ValueError("3D embedding failed — molecule may be too constrained")
    AllChem.MMFFOptimizeMolecule(mol_h)
    ctx.current_molecule = mol_h
    ctx.push_undo_checkpoint()
    ctx.enter_3d_viewer_mode()
    ctx.refresh_ui()
    return {"success": True}


def _find_moleditpy_spec() -> Any:
    """Return the importlib.util spec for the moleditpy package (tries both install names)."""
    import importlib.util  # pylint: disable=import-outside-toplevel
    for name in ("moleditpy", "moleditpy_linux"):
        spec = importlib.util.find_spec(name)
        if spec is not None and spec.submodule_search_locations:
            return spec
    raise ValueError(
        "moleditpy package not found in the current Python environment. "
        "Tried package names: moleditpy, moleditpy_linux."
    )


def _list_app_source_tree(args: Dict[str, Any]) -> Dict[str, Any]:
    from pathlib import Path  # pylint: disable=import-outside-toplevel
    spec = _find_moleditpy_spec()
    if spec is None or not spec.submodule_search_locations:
        raise ValueError("moleditpy package not found in the current Python environment")
    pkg_root = Path(spec.submodule_search_locations[0]).resolve()
    rel_path = args.get("path", "").strip()
    if rel_path:
        start = (pkg_root / rel_path).resolve()
        try:
            start.relative_to(pkg_root)
        except ValueError:
            raise ValueError(f"Path {rel_path!r} is outside the moleditpy package")
    else:
        start = pkg_root
    lines: List[str] = [f"{start.name}/  [{start}]"]
    _append_tree(start, "", lines)
    return {"content": "\n".join(lines)}


def _append_tree(directory: Any, prefix: str, lines: List[str]) -> None:
    from pathlib import Path  # pylint: disable=import-outside-toplevel
    skip = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache"}
    entries = sorted(
        [
            e for e in Path(directory).iterdir()
            if e.name not in skip and not e.name.endswith((".pyc", ".pyo"))
        ],
        key=lambda p: (p.is_file(), p.name.lower()),
    )
    for i, entry in enumerate(entries):
        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        if entry.is_dir():
            lines.append(f"{prefix}{connector}{entry.name}/")
            _append_tree(entry, prefix + ("    " if is_last else "│   "), lines)
        else:
            lines.append(
                f"{prefix}{connector}{entry.name}  ({entry.stat().st_size:,} bytes)"
            )


def _get_app_source_root() -> Dict[str, Any]:
    from pathlib import Path  # pylint: disable=import-outside-toplevel
    spec = _find_moleditpy_spec()
    if spec is None or not spec.submodule_search_locations:
        raise ValueError("moleditpy package not found in the current Python environment")
    return {"root": str(Path(spec.submodule_search_locations[0]).resolve())}


def _get_app_source(args: Dict[str, Any]) -> Dict[str, Any]:
    from pathlib import Path  # pylint: disable=import-outside-toplevel
    rel_path = args.get("path", "").strip()
    if not rel_path:
        raise ValueError("'path' argument is required")
    spec = _find_moleditpy_spec()
    if spec is None or not spec.submodule_search_locations:
        raise ValueError("moleditpy package not found in the current Python environment")
    pkg_root = Path(spec.submodule_search_locations[0]).resolve()
    target = (pkg_root / rel_path).resolve()
    try:
        target.relative_to(pkg_root)
    except ValueError:
        raise ValueError(f"Path {rel_path!r} is outside the moleditpy package")
    if target.is_dir():
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        lines = [f"Directory listing: {rel_path}"]
        for e in entries:
            lines.append(f"  {'[dir]' if e.is_dir() else '[file]'}  {e.name}"
                         + (f"  ({e.stat().st_size:,} bytes)" if e.is_file() else ""))
        return {"type": "directory", "content": "\n".join(lines)}
    if not target.exists():
        raise ValueError(f"{rel_path!r} does not exist in the moleditpy package")
    size = target.stat().st_size
    if size > 200 * 1024:
        raise ValueError(
            f"File is {size:,} bytes; exceeds the 200 KB read limit for source files"
        )
    return {"type": "file", "content": target.read_text(encoding="utf-8")}


def _run_python(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    import io  # pylint: disable=import-outside-toplevel
    import contextlib  # pylint: disable=import-outside-toplevel
    code = args.get("code", "").strip()
    if not code:
        raise ValueError("'code' argument is required")
    namespace: Dict[str, Any] = {"ctx": ctx, "result": None}
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        exec(code, namespace)  # noqa: S102
    return {
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
        "result": repr(namespace.get("result")),
    }


def _get_file_io_config(ctx: Any) -> Dict[str, Any]:
    base_dir = ctx.get_setting("file_io_base_dir", None)
    exts_raw = ctx.get_setting("file_io_allowed_extensions", None)
    allowed_exts = sorted(
        set(exts_raw) if exts_raw is not None else _DEFAULT_EXTENSIONS
    )
    return {"base_dir": base_dir, "allowed_extensions": allowed_exts}


def _set_file_io_config(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    if "base_dir" in args:
        ctx.set_setting("file_io_base_dir", args["base_dir"])
        ctx.show_status_message(
            f"MCP file I/O base directory set to: {args['base_dir']}", 5000
        )
    if "allowed_extensions" in args:
        exts = [e if e.startswith(".") else f".{e}" for e in args["allowed_extensions"]]
        ctx.set_setting("file_io_allowed_extensions", exts)
    return {"success": True}


# ---------------------------------------------------------------------------
# Molecule image
# ---------------------------------------------------------------------------


def _clamp_dimension(value: Any, default: int = 900) -> int:
    """Pixel dimensions are clamped, not validated away: a client guessing a
    huge canvas is far more likely than one trying to abuse the renderer, and
    clamping keeps the call useful instead of just failing it."""
    try:
        pixels = int(value)
    except (TypeError, ValueError):
        pixels = default
    return max(128, min(pixels, 2048))


def _get_molecule_image(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Render the current molecule to a PNG, base64-encoded for the MCP
    ``image`` content type.

    Both views are read straight off the public ``PluginContext`` API
    (``ctx.scene`` for the 2D canvas, ``ctx.plotter`` for the 3D viewer), so
    this never reaches past what a plugin is already allowed to touch.
    """
    view = (args.get("view") or "auto").strip().lower()
    if view not in ("auto", "2d", "3d"):
        raise ValueError("'view' must be 'auto', '2d', or '3d'")
    width = _clamp_dimension(args.get("width", 900))
    height = _clamp_dimension(args.get("height", 700))

    if view == "auto":
        mol = ctx.current_molecule
        has_3d = (
            ctx.plotter is not None and mol is not None and mol.GetNumConformers() > 0
        )
        view = "3d" if has_3d else "2d"

    png_bytes = _render_3d_png(ctx, width, height) if view == "3d" else _render_2d_png(
        ctx, width, height
    )
    if not png_bytes:
        empty = "3D viewer" if view == "3d" else "2D canvas"
        raise ValueError(
            f"Nothing to render: the {empty} is empty. Load a molecule first "
            + ("(trigger_3d_conversion may also be needed)." if view == "3d" else ".")
        )

    import base64  # pylint: disable=import-outside-toplevel

    return {
        "view": view,
        "width": width,
        "height": height,
        "mime_type": "image/png",
        "image_base64": base64.b64encode(png_bytes).decode("ascii"),
    }


def _render_2d_png(ctx: Any, width: int, height: int) -> Optional[bytes]:
    """The 2D canvas, rendered to PNG bytes via QGraphicsScene.render()."""
    # Before the Qt imports: "there is no canvas" is answerable without them,
    # and the environment that has no PyQt6 at all is exactly the one that
    # reaches this function with no scene.
    scene = ctx.scene
    if scene is None:
        return None

    from PyQt6.QtCore import QBuffer, QIODevice, QRectF  # pylint: disable=import-outside-toplevel
    from PyQt6.QtGui import QColor, QImage, QPainter  # pylint: disable=import-outside-toplevel

    source = scene.itemsBoundingRect()
    if source is None or source.isEmpty():
        return None
    # A small margin so atoms at the very edge of the bounding rect are not
    # clipped against the image border.
    margin = max(source.width(), source.height()) * 0.08 or 10.0
    source = source.adjusted(-margin, -margin, margin, margin)

    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    try:
        scene.render(painter, QRectF(0, 0, width, height), source)
    finally:
        painter.end()

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data or None


def _render_3d_png(ctx: Any, width: int, height: int) -> Optional[bytes]:
    """The 3D viewer, rendered to PNG bytes via the PyVista plotter.

    Through a temp file rather than ``return_img``: PyVista's in-memory array
    path returns RGB(A) pixels this function would then have to encode to PNG
    itself, while ``screenshot(path)`` already writes real PNG bytes -- one
    less place for a color-channel or byte-order mistake to hide.
    """
    import os  # pylint: disable=import-outside-toplevel
    import tempfile  # pylint: disable=import-outside-toplevel

    plotter = ctx.plotter
    if plotter is None:
        return None
    handle, path = tempfile.mkstemp(suffix=".png")
    os.close(handle)
    # PyVista's screenshot(window_size=...) assigns the size to the plotter and
    # never puts it back, so asking for an image would silently resize the
    # viewer the user is looking at -- and this tool is annotated read-only.
    previous = getattr(plotter, "window_size", None)
    try:
        plotter.screenshot(path, window_size=[width, height])
        with open(path, "rb") as file_obj:
            return file_obj.read() or None
    finally:
        if previous is not None:
            try:
                plotter.window_size = previous
            except Exception:  # pylint: disable=broad-except
                # A plotter that will not take its own size back is not a
                # reason to fail a screenshot that already succeeded.
                logging.debug("MCP Server: 3D viewer size not restored")
        try:
            os.unlink(path)
        except OSError:
            logging.debug("MCP Server: temp screenshot not removed: %s", path)


# ---------------------------------------------------------------------------
# Molecule manipulation (direct RDKit access)
# ---------------------------------------------------------------------------


def _get_molecule_descriptors(ctx: Any) -> Dict[str, Any]:
    """Common RDKit descriptors for the current molecule, in one call."""
    mol = ctx.current_molecule
    if mol is None:
        return {"loaded": False}
    from rdkit import Chem  # pylint: disable=import-outside-toplevel
    from rdkit.Chem import Crippen, Descriptors, rdMolDescriptors  # pylint: disable=import-outside-toplevel

    return {
        "loaded": True,
        "canonical_smiles": Chem.MolToSmiles(mol),
        "formula": rdMolDescriptors.CalcMolFormula(mol),
        "molecular_weight": round(Descriptors.MolWt(mol), 4),
        "exact_mass": round(Descriptors.ExactMolWt(mol), 4),
        "logp": round(Crippen.MolLogP(mol), 4),
        "tpsa": round(rdMolDescriptors.CalcTPSA(mol), 4),
        "formal_charge": Chem.GetFormalCharge(mol),
        "num_h_donors": rdMolDescriptors.CalcNumHBD(mol),
        "num_h_acceptors": rdMolDescriptors.CalcNumHBA(mol),
        "num_rotatable_bonds": rdMolDescriptors.CalcNumRotatableBonds(mol),
        "num_rings": rdMolDescriptors.CalcNumRings(mol),
        "num_aromatic_rings": rdMolDescriptors.CalcNumAromaticRings(mol),
        "num_atoms": mol.GetNumAtoms(),
        "num_heavy_atoms": mol.GetNumHeavyAtoms(),
        "num_bonds": mol.GetNumBonds(),
    }


def _add_hydrogens(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Add explicit hydrogens to the current molecule (RDKit AddHs)."""
    mol = ctx.current_molecule
    if mol is None:
        raise ValueError("No molecule loaded")
    from rdkit import Chem  # pylint: disable=import-outside-toplevel

    explicit_only = bool(args.get("explicit_only", False))
    mol_h = Chem.AddHs(
        mol, explicitOnly=explicit_only, addCoords=mol.GetNumConformers() > 0
    )
    ctx.current_molecule = mol_h
    ctx.push_undo_checkpoint()
    ctx.refresh_ui()
    return {"success": True, "num_atoms": mol_h.GetNumAtoms()}


def _remove_hydrogens(ctx: Any) -> Dict[str, Any]:
    """Strip explicit hydrogens from the current molecule (RDKit RemoveHs)."""
    mol = ctx.current_molecule
    if mol is None:
        raise ValueError("No molecule loaded")
    from rdkit import Chem  # pylint: disable=import-outside-toplevel

    mol_no_h = Chem.RemoveHs(mol)
    ctx.current_molecule = mol_no_h
    ctx.push_undo_checkpoint()
    ctx.refresh_ui()
    return {"success": True, "num_atoms": mol_no_h.GetNumAtoms()}


def _optimize_geometry(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Minimize the current 3D conformer with MMFF94 or UFF.

    Distinct from trigger_3d_conversion, which *generates* a fresh conformer:
    this refines coordinates the molecule already has, so it errors rather
    than silently embedding new ones when there is nothing to refine.
    """
    mol = ctx.current_molecule
    if mol is None:
        raise ValueError("No molecule loaded")
    if mol.GetNumConformers() == 0:
        raise ValueError(
            "Molecule has no 3D coordinates to optimize. Run trigger_3d_conversion first."
        )
    force_field = (args.get("force_field") or "mmff").strip().lower()
    if force_field not in ("mmff", "uff"):
        raise ValueError("'force_field' must be 'mmff' or 'uff'")
    max_iters = int(args.get("max_iters", 500))

    from rdkit import Chem  # pylint: disable=import-outside-toplevel
    from rdkit.Chem import AllChem  # pylint: disable=import-outside-toplevel

    working = Chem.Mol(mol)
    if force_field == "mmff":
        status = AllChem.MMFFOptimizeMolecule(working, maxIters=max_iters)
    else:
        status = AllChem.UFFOptimizeMolecule(working, maxIters=max_iters)
    ctx.current_molecule = working
    ctx.push_undo_checkpoint()
    ctx.refresh_3d_view()
    ctx.refresh_ui()
    return {"success": True, "force_field": force_field, "converged": status == 0}


def _set_atom_charge(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Set one atom's formal charge, re-sanitizing before it is accepted."""
    mol = ctx.current_molecule
    if mol is None:
        raise ValueError("No molecule loaded")
    if "atom_index" not in args:
        raise ValueError("'atom_index' argument is required")
    if "charge" not in args:
        raise ValueError("'charge' argument is required")
    atom_index = int(args["atom_index"])
    charge = int(args["charge"])
    num_atoms = mol.GetNumAtoms()
    if atom_index < 0 or atom_index >= num_atoms:
        raise ValueError(f"atom_index {atom_index} is out of range (0-{num_atoms - 1})")

    from rdkit import Chem  # pylint: disable=import-outside-toplevel

    working = Chem.RWMol(mol)
    working.GetAtomWithIdx(atom_index).SetFormalCharge(charge)
    new_mol = working.GetMol()
    try:
        Chem.SanitizeMol(new_mol)
    except Exception as exc:  # pylint: disable=broad-except
        raise ValueError(
            f"Setting charge {charge} on atom {atom_index} produced an invalid "
            f"molecule: {exc}"
        ) from exc
    ctx.current_molecule = new_mol
    ctx.push_undo_checkpoint()
    ctx.refresh_ui()
    return {"success": True, "atom_index": atom_index, "charge": charge}


def _delete_atoms(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Remove one or more atoms by index, highest index first.

    Highest-first matters: removing a lower index first would shift every
    atom above it down by one, so the caller's remaining indices would no
    longer name the atoms they were chosen against.
    """
    mol = ctx.current_molecule
    if mol is None:
        raise ValueError("No molecule loaded")
    atom_indices = args.get("atom_indices")
    if not atom_indices:
        raise ValueError("'atom_indices' argument is required")
    num_atoms = mol.GetNumAtoms()
    indices = sorted({int(i) for i in atom_indices}, reverse=True)
    for idx in indices:
        if idx < 0 or idx >= num_atoms:
            raise ValueError(f"atom_index {idx} is out of range (0-{num_atoms - 1})")

    from rdkit import Chem  # pylint: disable=import-outside-toplevel

    working = Chem.RWMol(mol)
    for idx in indices:
        working.RemoveAtom(idx)
    new_mol = working.GetMol()
    try:
        Chem.SanitizeMol(new_mol)
    except Exception as exc:  # pylint: disable=broad-except
        raise ValueError(f"Deleting atom(s) {indices} produced an invalid molecule: {exc}") from exc
    ctx.current_molecule = new_mol
    ctx.push_undo_checkpoint()
    ctx.refresh_ui()
    return {
        "success": True,
        "deleted": sorted(indices),
        "remaining_atoms": new_mol.GetNumAtoms(),
    }


def _substructure_search(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """SMARTS substructure matches against the current molecule (read-only)."""
    mol = ctx.current_molecule
    if mol is None:
        return {"loaded": False, "matches": []}
    smarts = (args.get("smarts") or "").strip()
    if not smarts:
        raise ValueError("'smarts' argument is required")

    from rdkit import Chem  # pylint: disable=import-outside-toplevel

    pattern = Chem.MolFromSmarts(smarts)
    if pattern is None:
        raise ValueError(f"Invalid SMARTS pattern: {smarts!r}")
    unique = bool(args.get("unique_matches", True))
    matches = mol.GetSubstructMatches(pattern, uniquify=unique)
    return {
        "loaded": True,
        "smarts": smarts,
        "num_matches": len(matches),
        "matches": [list(match) for match in matches],
    }


def _compute_partial_charges(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Gasteiger partial charges per atom. Never mutates the live molecule."""
    mol = ctx.current_molecule
    if mol is None:
        raise ValueError("No molecule loaded")
    atom_indices = args.get("atom_indices") or []

    from rdkit import Chem  # pylint: disable=import-outside-toplevel
    from rdkit.Chem import AllChem  # pylint: disable=import-outside-toplevel

    working = Chem.Mol(mol)
    AllChem.ComputeGasteigerCharges(working)
    wanted = set(int(i) for i in atom_indices) if atom_indices else None
    charges = []
    for atom in working.GetAtoms():
        idx = atom.GetIdx()
        if wanted is not None and idx not in wanted:
            continue
        raw = atom.GetDoubleProp("_GasteigerCharge") if atom.HasProp("_GasteigerCharge") else 0.0
        charges.append({"index": idx, "symbol": atom.GetSymbol(), "charge": round(raw, 4)})
    return {"charges": charges}


def _plugin_version() -> str:
    """Plugin version, resolved without assuming the package's mounted name."""
    # The loader may mount this package as e.g. ``AI.mcp_server`` (subfolder install),
    # so a plain ``import mcp_server`` is not guaranteed to resolve.
    try:
        from . import PLUGIN_VERSION  # pylint: disable=import-outside-toplevel

        return PLUGIN_VERSION
    except ImportError:
        pass
    try:
        from mcp_server import PLUGIN_VERSION  # pylint: disable=import-outside-toplevel

        return PLUGIN_VERSION
    except ImportError:
        return "unknown"


def _app_version(mw: Any) -> str:
    """MoleditPy version — the main window exposes none, so fall back to the package."""
    if mw is not None:
        version = getattr(mw, "VERSION", None)
        if isinstance(version, str) and version:
            return version
        settings = getattr(getattr(mw, "init_manager", None), "settings", None)
        if isinstance(settings, dict):
            version = settings.get("app_version")
            if isinstance(version, str) and version:
                return version
    try:
        # Canonical source: moleditpy.utils.constants.VERSION
        from moleditpy import __version__  # pylint: disable=import-outside-toplevel

        if isinstance(__version__, str) and __version__:
            return __version__
    except ImportError:
        pass
    return "unknown"


def _get_app_info(ctx: Any) -> Dict[str, Any]:
    return {
        "app": "MoleditPy",
        "version": _app_version(ctx.get_main_window()),
        "mcp_plugin_version": _plugin_version(),
    }


# ---------------------------------------------------------------------------
# Qt bridge (wraps execute_operation with cross-thread signal machinery)
# ---------------------------------------------------------------------------


class MCPBridge(QObject):
    """
    Forwards PluginContext calls from a background thread to the Qt main thread.

    Usage::

        bridge = MCPBridge(context)                 # on main thread
        result = bridge.call("get_molecule_info")   # from server thread
    """

    # Carries (operation_name, args_dict, result_container).
    # QueuedConnection is activated automatically when the signal is emitted
    # from a thread other than the one that owns this QObject.
    _request = pyqtSignal(str, object, object)

    def __init__(self, context: Any, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._context = context
        self._request.connect(self._on_request, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------------
    # Public API (called from background threads)
    # ------------------------------------------------------------------

    def call(
        self,
        operation: str,
        args: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> Any:
        """
        Execute *operation* on the Qt main thread and return its result.

        Blocks the calling thread until the result is available or *timeout*
        seconds have elapsed (raises ``TimeoutError`` in the latter case).
        """
        if args is None:
            args = {}
        container: Dict[str, Any] = {
            "event": threading.Event(),
            "result": None,
            "error": None,
        }
        self._request.emit(operation, args, container)
        if not container["event"].wait(timeout):
            raise TimeoutError(
                f"Operation {operation!r} timed out after {timeout}s"
            )
        if container["error"] is not None:
            raise container["error"]
        return container["result"]

    # ------------------------------------------------------------------
    # Private slot (runs on Qt main thread)
    # ------------------------------------------------------------------

    def _on_request(
        self,
        operation: str,
        args: object,
        container: object,
    ) -> None:
        """Execute the requested operation and signal completion."""
        c: Dict[str, Any] = container  # type: ignore[assignment]
        try:
            c["result"] = execute_operation(
                self._context, operation, dict(args)  # type: ignore[arg-type]
            )
        except Exception as exc:  # pylint: disable=broad-except
            c["error"] = exc
        finally:
            c["event"].set()

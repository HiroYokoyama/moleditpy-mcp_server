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

import contextlib
import logging
import math
import threading
from typing import Any, Dict, Iterator, List, Optional

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
        charge = args.get("charge")
        if charge is not None:
            if isinstance(charge, bool) or not isinstance(charge, int):
                raise ValueError("'charge' must be an integer")
        skip = bool(args.get("skip_chemistry", False))
        frame = args.get("frame")
        if frame is not None:
            frame = _int_arg(frame, "frame")
        xyz_text, frame_idx, n_frames = _select_xyz_frame(xyz_text, frame)
        plotter = ctx.plotter if args.get("keep_camera") else None
        camera = plotter.camera_position if plotter is not None else None
        with _xyz_charge_override(ctx, charge, skip) as state:
            mol = ctx.show_xyz_data(xyz_text, source_name=source_name)
        if camera is not None and mol is not None:
            plotter.camera_position = camera
            plotter.render()
        result = _show_xyz_result(mol, charge, state)
        if n_frames > 1:
            result.update(frame=frame_idx, num_frames=n_frames)
        return result

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
        if not isinstance(atom_colors, dict):
            raise ValueError("'atom_colors' must be an object mapping atom index to color")
        ctrl = ctx.get_3d_controller()
        if ctrl is None:
            raise ValueError("3D controller is not available (is the 3D viewer active?)")
        # Parse every key before coloring any atom, so a bad key leaves the
        # view untouched instead of half-applied.
        resolved = {_int_arg(idx, "atom index"): color for idx, color in atom_colors.items()}
        for idx, color in resolved.items():
            ctrl.set_atom_color(idx, color)
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

    if operation == "get_3d_camera":
        return _get_3d_camera(ctx)

    if operation == "set_3d_camera":
        return _set_3d_camera(ctx, args)

    if operation == "measure_geometry":
        return _measure_geometry(ctx, args)

    if operation == "compare_structures":
        return _compare_structures(ctx, args)

    if operation == "clear_overlay":
        return _clear_overlay(ctx)

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


def _int_arg(value: Any, what: str) -> int:
    """``int(value)`` with an error message that names the argument."""
    if isinstance(value, bool):
        raise ValueError(f"Invalid {what} {value!r}: expected an integer.")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {what} {value!r}: expected an integer.") from None


def _check_atom_index(mol: Any, value: Any) -> int:
    """Validate *value* as an atom index of *mol*.

    RDKit's own out-of-range error is a multi-line C++ "Range Error" dump that
    tells the client nothing about which index was wrong or what the valid
    range is.
    """
    idx = _int_arg(value, "atom index")
    num_atoms = mol.GetNumAtoms()
    if idx < 0 or idx >= num_atoms:
        raise ValueError(f"atom_index {idx} is out of range (0-{num_atoms - 1})")
    return idx


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
        resolved[_int_arg(idx_str, "bond index")] = color

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
    if atom_indices:
        atom_indices = [_check_atom_index(mol, i) for i in atom_indices]
    else:
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


_BOND_TYPE_NAMES = {
    1.0: "SINGLE",
    2.0: "DOUBLE",
    3.0: "TRIPLE",
    1.5: "AROMATIC",
}


def _get_bond_info(ctx: Any) -> Dict[str, Any]:
    mol = ctx.current_molecule
    if mol is None:
        return {"bonds": []}
    bonds: List[Dict[str, Any]] = []
    for bond in mol.GetBonds():
        bond_order = bond.GetBondTypeAsDouble()
        bonds.append(
            {
                "index": bond.GetIdx(),
                "atom1": bond.GetBeginAtomIdx(),
                "atom2": bond.GetEndAtomIdx(),
                "bond_type": _BOND_TYPE_NAMES.get(bond_order, str(bond_order)),
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


_XYZ_SETTING_KEYS = ("skip_chemistry_checks", "always_ask_charge")
_MISSING = object()


@contextlib.contextmanager
def _xyz_charge_override(ctx: Any, charge: Optional[int], skip: bool) -> Iterator[Dict[str, Any]]:
    """Answer MoleditPy's XYZ charge prompt on the caller's behalf.

    Loading XYZ text first tries bond perception with charge 0 and, when that
    fails (or 'always ask' is on), opens a modal charge dialog. Over MCP no
    one is there to answer it, so for the duration of the call the prompt is
    replaced by one that returns *charge* once and then "skip chemistry"
    (a second prompt means perception failed with that charge; looping on the
    same answer would never end). An explicit *charge* also bypasses the
    silent charge-0 attempt, which can "succeed" with wrong bond orders for
    an ion. The app's settings and prompt are restored afterwards.
    """
    state: Dict[str, Any] = {"prompts": 0, "fallback": False}
    mw = ctx.get_main_window() if hasattr(ctx, "get_main_window") else None
    io_mgr = getattr(mw, "io_manager", None)
    settings = getattr(getattr(mw, "init_manager", None), "settings", None)
    if io_mgr is None or not isinstance(settings, dict):
        yield state
        return

    def _prompt() -> tuple:
        state["prompts"] += 1
        if charge is not None and state["prompts"] == 1:
            return charge, True, False
        state["fallback"] = True
        return 0, True, True

    own = vars(io_mgr) if hasattr(io_mgr, "__dict__") else {}
    saved_prompt = own.get("prompt_for_charge", _MISSING)
    saved = {k: settings.get(k, _MISSING) for k in _XYZ_SETTING_KEYS}
    io_mgr.prompt_for_charge = _prompt
    if skip:
        settings["skip_chemistry_checks"] = True
    elif charge is not None:
        settings["skip_chemistry_checks"] = False
        settings["always_ask_charge"] = True
    try:
        yield state
    finally:
        if saved_prompt is _MISSING:
            try:
                del io_mgr.prompt_for_charge
            except AttributeError:
                pass
        else:
            io_mgr.prompt_for_charge = saved_prompt
        for key, value in saved.items():
            if value is _MISSING:
                settings.pop(key, None)
            else:
                settings[key] = value


def _show_xyz_result(mol: Any, charge: Optional[int], state: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize how an XYZ load went: which charge was used, whether bond
    perception was skipped (distance-based bonds only), and why."""
    if mol is None:
        return {"success": False}
    skipped = bool(mol.HasProp("_xyz_skip_checks") and mol.GetIntProp("_xyz_skip_checks"))
    result: Dict[str, Any] = {
        "success": True,
        "chemistry_skipped": skipped,
        "charge": None if skipped else (
            mol.GetIntProp("_xyz_charge") if mol.HasProp("_xyz_charge") else charge
        ),
        "num_atoms": mol.GetNumAtoms(),
        "num_bonds": mol.GetNumBonds(),
    }
    if state.get("fallback"):
        result["note"] = (
            f"Bond perception failed with charge {charge}; loaded with distance-based bonds."
            if charge is not None
            else "Bond perception failed with charge 0; loaded with distance-based bonds. "
            "Pass 'charge' for ions."
        )
    return result


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
    matched = False
    for candidate in (Chem.AddHs(mol), mol):
        products = rxn.RunReactants((candidate,))
        if not products:
            continue
        matched = True
        selected = _select_product_by_anchor(
            rxn, candidate, products, args.get("atom_index")
        )
        clean_mol = _clean_reaction_product(products[selected][0])
        if clean_mol is not None:
            attempt = (products, selected, clean_mol)
            break

    if attempt is None:
        # "Did not match" only when neither attempt matched: an explicit-H
        # match whose product failed sanitization, followed by an implicit-H
        # attempt that matched nothing, is a bad product, not a bad pattern.
        if not matched:
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


def _moleditpy_pkg_root() -> Any:
    """Resolved root directory of the installed moleditpy package."""
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    return Path(_find_moleditpy_spec().submodule_search_locations[0]).resolve()


def _resolve_in_package(pkg_root: Any, rel_path: str) -> Any:
    """Resolve *rel_path* under *pkg_root*, refusing anything that escapes it."""
    target = (pkg_root / rel_path).resolve()
    try:
        target.relative_to(pkg_root)
    except ValueError:
        raise ValueError(f"Path {rel_path!r} is outside the moleditpy package") from None
    return target


def _list_app_source_tree(args: Dict[str, Any]) -> Dict[str, Any]:
    pkg_root = _moleditpy_pkg_root()
    rel_path = (args.get("path") or "").strip()
    start = _resolve_in_package(pkg_root, rel_path) if rel_path else pkg_root
    if not start.is_dir():
        raise ValueError(f"{rel_path!r} is not a directory in the moleditpy package")
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
    return {"root": str(_moleditpy_pkg_root())}


def _get_app_source(args: Dict[str, Any]) -> Dict[str, Any]:
    rel_path = (args.get("path") or "").strip()
    if not rel_path:
        raise ValueError("'path' argument is required")
    target = _resolve_in_package(_moleditpy_pkg_root(), rel_path)
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
        # Fully validated by the server (normalize_extensions); the '.'
        # prefix is kept here too for direct callers.
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

    if view == "3d":
        with _atom_index_labels(ctx, bool(args.get("atom_labels", False))):
            png_bytes = _render_3d_png(ctx, width, height)
    else:
        png_bytes = _render_2d_png(ctx, width, height)
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
                logger.debug("3D viewer size not restored")
        try:
            os.unlink(path)
        except OSError:
            logger.debug("Temp screenshot not removed: %s", path)


@contextlib.contextmanager
def _atom_index_labels(ctx: Any, enabled: bool) -> Iterator[None]:
    """Overlay 0-based atom indices on the 3D viewer for one capture, then
    remove them so the user's view is left as it was."""
    plotter = ctx.plotter
    mol = ctx.current_molecule
    if not enabled or plotter is None or mol is None or mol.GetNumConformers() == 0:
        yield
        return
    conf = mol.GetConformer()
    points = [list(conf.GetAtomPosition(i)) for i in range(mol.GetNumAtoms())]
    actor = plotter.add_point_labels(
        points,
        [str(i) for i in range(len(points))],
        font_size=14,
        text_color="black",
        shape_color="white",
        shape_opacity=0.6,
        show_points=False,
        always_visible=True,
        name="_mcp_atom_index_labels",
    )
    try:
        yield
    finally:
        try:
            plotter.remove_actor(actor)
            plotter.render()
        except Exception:  # pylint: disable=broad-except
            logger.debug("Atom index labels not removed")


# ---------------------------------------------------------------------------
# 3D camera
# ---------------------------------------------------------------------------


def _vec3(value: Any, what: str) -> List[float]:
    """A finite 3-vector from a JSON list, or ValueError naming the argument."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"'{what}' must be a list of 3 numbers")
    try:
        out = [float(v) for v in value]
    except (TypeError, ValueError):
        raise ValueError(f"'{what}' must be a list of 3 numbers") from None
    if not all(math.isfinite(v) for v in out):
        raise ValueError(f"'{what}' must contain finite numbers")
    return out


def _camera_state(plotter: Any) -> Dict[str, Any]:
    pos, focal, up = plotter.camera_position
    return {
        "position": [round(float(v), 4) for v in pos],
        "focal_point": [round(float(v), 4) for v in focal],
        "view_up": [round(float(v), 4) for v in up],
    }


def _get_3d_camera(ctx: Any) -> Dict[str, Any]:
    plotter = ctx.plotter
    if plotter is None:
        raise ValueError("The 3D viewer is not available.")
    return _camera_state(plotter)


def _set_3d_camera(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Point the 3D camera explicitly.

    Either give ``position`` (absolute), or ``direction`` (from the focal
    point toward the camera, i.e. the side you look from). ``fit`` (default
    true with ``direction``) re-frames the molecule while keeping that
    orientation; ``zoom`` > 1 then moves in.
    """
    plotter = ctx.plotter
    if plotter is None:
        raise ValueError("The 3D viewer is not available.")
    if "position" in args and "direction" in args:
        raise ValueError("Pass either 'position' or 'direction', not both")
    cur_pos, cur_focal, cur_up = (list(map(float, v)) for v in plotter.camera_position)
    focal = _vec3(args["focal_point"], "focal_point") if "focal_point" in args else cur_focal
    up = _vec3(args["view_up"], "view_up") if "view_up" in args else cur_up
    if "position" in args:
        pos = _vec3(args["position"], "position")
    elif "direction" in args:
        d = _vec3(args["direction"], "direction")
        norm = math.sqrt(sum(v * v for v in d))
        if norm == 0.0:
            raise ValueError("'direction' must be non-zero")
        dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(cur_pos, cur_focal))) or 10.0
        pos = [f + dist * v / norm for f, v in zip(focal, d)]
    else:
        pos = cur_pos
    view = [p - f for p, f in zip(pos, focal)]
    view_len = math.sqrt(sum(v * v for v in view))
    up_len = math.sqrt(sum(v * v for v in up))
    if view_len == 0.0 or up_len == 0.0:
        raise ValueError("Camera position must differ from focal_point, and view_up be non-zero")
    cos = abs(sum(a * b for a, b in zip(view, up))) / (view_len * up_len)
    if cos > 0.999:
        raise ValueError("'view_up' is parallel to the viewing direction")
    plotter.camera_position = [pos, focal, up]
    fit = args.get("fit", "direction" in args)
    if fit:
        plotter.reset_camera()
    zoom = args.get("zoom")
    if zoom is not None:
        zoom = float(zoom)
        if not (math.isfinite(zoom) and zoom > 0):
            raise ValueError("'zoom' must be a positive number")
        plotter.camera.zoom(zoom)
    plotter.render()
    return _camera_state(plotter)


# ---------------------------------------------------------------------------
# Geometry measurement
# ---------------------------------------------------------------------------


def _sub(a: List[float], b: List[float]) -> List[float]:
    return [x - y for x, y in zip(a, b)]


def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _cross(a: List[float], b: List[float]) -> List[float]:
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _angle_deg(a: List[float], b: List[float], c: List[float]) -> float:
    """Angle a-b-c in degrees."""
    u, v = _sub(a, b), _sub(c, b)
    nu, nv = math.sqrt(_dot(u, u)), math.sqrt(_dot(v, v))
    if nu == 0.0 or nv == 0.0:
        raise ValueError("Angle undefined: two atoms coincide")
    return math.degrees(math.acos(max(-1.0, min(1.0, _dot(u, v) / (nu * nv)))))


def _dihedral_deg(a: List[float], b: List[float], c: List[float], d: List[float]) -> float:
    """Signed dihedral a-b-c-d in degrees (IUPAC sign convention)."""
    b0, b1, b2 = _sub(a, b), _sub(c, b), _sub(d, c)
    n1 = math.sqrt(_dot(b1, b1))
    if n1 == 0.0:
        raise ValueError("Dihedral undefined: the central atoms coincide")
    b1 = [x / n1 for x in b1]
    v = _sub(b0, [_dot(b0, b1) * x for x in b1])
    w = _sub(b2, [_dot(b2, b1) * x for x in b1])
    if _dot(v, v) == 0.0 or _dot(w, w) == 0.0:
        raise ValueError("Dihedral undefined: three atoms are collinear")
    return math.degrees(math.atan2(_dot(_cross(b1, v), w), _dot(v, w)))


def _measure_geometry(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """Distances (2 atoms), angles (3) and dihedrals (4) on the current 3D
    conformer, by 0-based atom index."""
    mol = ctx.current_molecule
    if mol is None or mol.GetNumConformers() == 0:
        raise ValueError("No 3D coordinates available.")
    groups = args.get("atoms")
    if not isinstance(groups, list) or not groups:
        raise ValueError("'atoms' must be a non-empty list of index lists")
    if len(groups) > 500:
        raise ValueError("At most 500 measurements per call")
    conf = mol.GetConformer()
    out = []
    for group in groups:
        if not isinstance(group, list) or len(group) not in (2, 3, 4):
            raise ValueError("Each entry must list 2, 3 or 4 atom indices")
        idx = [_check_atom_index(mol, i) for i in group]
        if len(set(idx)) != len(idx):
            raise ValueError(f"Repeated atom index in {group}")
        pts = [[float(v) for v in conf.GetAtomPosition(i)] for i in idx]
        if len(idx) == 2:
            kind, value = "distance", math.dist(pts[0], pts[1])
        elif len(idx) == 3:
            kind, value = "angle", _angle_deg(*pts)
        else:
            kind, value = "dihedral", _dihedral_deg(*pts)
        symbols = [mol.GetAtomWithIdx(i).GetSymbol() for i in idx]
        out.append({"atoms": idx, "symbols": symbols, "type": kind, "value": round(float(value), 4)})
    return {"measurements": out, "units": {"distance": "angstrom", "angle": "degree", "dihedral": "degree"}}


# ---------------------------------------------------------------------------
# Structure comparison (RMSD + overlay)
# ---------------------------------------------------------------------------

_OVERLAY_NAMES = ("_mcp_overlay_atoms", "_mcp_overlay_bonds")


def split_xyz_blocks(text: str) -> List[tuple]:
    """Split XYZ text into frames of (comment, [atom lines]), raw lines kept.

    Multi-frame files (optimization trajectories) are the standard
    "count / comment / atoms" blocks back to back. Text without a count
    header is one frame of bare 'Element X Y Z' lines with an empty comment.
    """
    lines = text.splitlines()
    first = next((ln for ln in lines if ln.strip()), "")
    if not first.strip().isdigit():
        atoms = [ln for ln in lines if ln.strip()]
        return [("", atoms)] if atoms else []
    frames: List[tuple] = []
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        count_text = lines[i].strip()
        if not count_text.isdigit():
            raise ValueError(f"Expected an atom count on line {i + 1}, got {count_text!r}")
        count = int(count_text)
        comment = lines[i + 1] if i + 1 < len(lines) else ""
        block = lines[i + 2:i + 2 + count]
        if len(block) < count:
            raise ValueError(f"Frame {len(frames)} is truncated ({len(block)} of {count} atoms)")
        frames.append((comment, block))
        i += 2 + count
    return frames


def parse_xyz_frames(text: str) -> List[List[tuple]]:
    """Frames of (symbol, x, y, z) tuples; see split_xyz_blocks."""
    return [[_xyz_row(ln) for ln in block] for _comment, block in split_xyz_blocks(text)]


def _select_xyz_frame(text: str, frame: Optional[int]) -> tuple:
    """(xyz text of one frame, frame index, frame count).

    Without *frame* a single-frame text is passed through untouched (the
    app's own parser handles its quirks); a trajectory defaults to its last
    frame, the converged geometry of an optimization.
    """
    try:
        blocks = split_xyz_blocks(text)
    except ValueError:
        if frame is None:
            return text, 0, 1
        raise
    if not blocks:
        return text, 0, 0
    if frame is None and len(blocks) == 1:
        return text, 0, 1
    idx = len(blocks) - 1 if frame is None else frame
    if not -len(blocks) <= idx < len(blocks):
        raise ValueError(f"'frame' {frame} out of range ({len(blocks)} frames)")
    idx %= len(blocks)
    comment, atoms = blocks[idx]
    return "\n".join([str(len(atoms)), comment] + atoms), idx, len(blocks)


def _xyz_row(line: str) -> tuple:
    parts = line.split()
    if len(parts) < 4:
        raise ValueError(f"Not an 'Element X Y Z' line: {line!r}")
    symbol = parts[0].strip().capitalize()
    try:
        return (symbol, float(parts[1]), float(parts[2]), float(parts[3]))
    except ValueError:
        raise ValueError(f"Not an 'Element X Y Z' line: {line!r}") from None


def _kabsch(p: Any, q: Any) -> tuple:
    """Rotation R and translation t minimizing |(q @ R.T + t) - p| (moves q onto p)."""
    import numpy as np  # pylint: disable=import-outside-toplevel

    pc, qc = p.mean(axis=0), q.mean(axis=0)
    h = (q - qc).T @ (p - pc)
    u, _s, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T)) or 1.0
    rot = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    return rot, pc - qc @ rot.T


def _compare_structures(ctx: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """RMSD between the current 3D molecule and another structure with the
    same atoms in the same order, optionally drawn as a translucent overlay."""
    import numpy as np  # pylint: disable=import-outside-toplevel

    mol = ctx.current_molecule
    if mol is None or mol.GetNumConformers() == 0:
        raise ValueError("No 3D coordinates available for the current molecule.")
    frames = parse_xyz_frames(args.get("xyz_text", ""))
    if not frames:
        raise ValueError("'xyz_text' contains no atoms")
    frame_idx = int(args.get("frame", -1))
    try:
        other = frames[frame_idx]
    except IndexError:
        raise ValueError(f"'frame' {frame_idx} out of range ({len(frames)} frames)") from None
    n = mol.GetNumAtoms()
    if len(other) != n:
        raise ValueError(f"Atom count differs: current {n}, other {len(other)}")
    symbols = [mol.GetAtomWithIdx(i).GetSymbol() for i in range(n)]
    mismatch = [i for i in range(n) if symbols[i] != other[i][0]]
    if mismatch:
        i = mismatch[0]
        raise ValueError(
            f"Atom order differs: index {i} is {symbols[i]} here and {other[i][0]} in the other structure"
        )
    conf = mol.GetConformer()
    p = np.array([list(conf.GetAtomPosition(i)) for i in range(n)])
    q = np.array([row[1:] for row in other])
    heavy_only = bool(args.get("heavy_atoms_only", False))
    sel = [i for i in range(n) if not (heavy_only and symbols[i] == "H")] or list(range(n))
    if args.get("align", True):
        rot, t = _kabsch(p[sel], q[sel])
        q = q @ rot.T + t
    dev = np.linalg.norm(p - q, axis=1)
    rmsd = float(np.sqrt((dev[sel] ** 2).mean()))
    order = sorted(sel, key=lambda i: -dev[i])[:10]
    result = {
        "rmsd": round(rmsd, 4),
        "atoms_used": len(sel),
        "aligned": bool(args.get("align", True)),
        "largest_deviations": [
            {"index": i, "symbol": symbols[i], "deviation": round(float(dev[i]), 4)} for i in order
        ],
    }
    if args.get("overlay", False):
        _draw_overlay(ctx, mol, q, str(args.get("overlay_color", "orange")))
        result["overlay"] = True
    return result


def _draw_overlay(ctx: Any, mol: Any, coords: Any, color: str) -> None:
    """Translucent spheres + bonds for the other structure, using the current
    molecule's bonds (same atom order was checked by the caller)."""
    import numpy as np  # pylint: disable=import-outside-toplevel
    import pyvista as pv  # pylint: disable=import-outside-toplevel

    plotter = ctx.plotter
    if plotter is None:
        raise ValueError("The 3D viewer is not available.")
    _clear_overlay(ctx)
    plotter.add_mesh(
        pv.PolyData(np.asarray(coords, dtype=float)),
        color=color, opacity=0.5, point_size=14, render_points_as_spheres=True,
        name=_OVERLAY_NAMES[0], pickable=False,
    )
    bonds = [(b.GetBeginAtomIdx(), b.GetEndAtomIdx()) for b in mol.GetBonds()]
    if bonds:
        lines = np.hstack([[2, a, b] for a, b in bonds])
        plotter.add_mesh(
            pv.PolyData(np.asarray(coords, dtype=float), lines=lines),
            color=color, opacity=0.5, line_width=4, name=_OVERLAY_NAMES[1], pickable=False,
        )
    plotter.render()


def _clear_overlay(ctx: Any) -> Dict[str, Any]:
    plotter = ctx.plotter
    removed = 0
    if plotter is not None:
        for name in _OVERLAY_NAMES:
            try:
                if plotter.remove_actor(name):
                    removed += 1
            except Exception:  # pylint: disable=broad-except
                logger.debug("Overlay actor %s not removed", name)
        plotter.render()
    return {"removed": removed}


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
    max_iters = _int_arg(args.get("max_iters", 500), "max_iters")
    if max_iters < 1:
        raise ValueError("'max_iters' must be at least 1")

    from rdkit import Chem  # pylint: disable=import-outside-toplevel
    from rdkit.Chem import AllChem  # pylint: disable=import-outside-toplevel

    working = Chem.Mol(mol)
    if force_field == "mmff":
        status = AllChem.MMFFOptimizeMolecule(working, maxIters=max_iters)
    else:
        status = AllChem.UFFOptimizeMolecule(working, maxIters=max_iters)
    if status == -1:
        # RDKit's "force field could not be set up" (e.g. an element MMFF94 has
        # no parameters for). Nothing moved, so there is nothing to commit.
        raise ValueError(
            f"{force_field.upper()} could not be set up for this molecule "
            "(missing force-field parameters). "
            + ("Try force_field='uff'." if force_field == "mmff" else "")
        )
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

    import math  # pylint: disable=import-outside-toplevel

    working = Chem.Mol(mol)
    AllChem.ComputeGasteigerCharges(working)
    wanted = {_check_atom_index(mol, i) for i in atom_indices} if atom_indices else None
    charges = []
    for atom in working.GetAtoms():
        idx = atom.GetIdx()
        if wanted is not None and idx not in wanted:
            continue
        raw = atom.GetDoubleProp("_GasteigerCharge") if atom.HasProp("_GasteigerCharge") else 0.0
        # Gasteiger has no parameters for some elements (metals, B, ...) and
        # yields NaN there. NaN is not JSON, so report "no charge" instead.
        charge = round(raw, 4) if math.isfinite(raw) else None
        charges.append({"index": idx, "symbol": atom.GetSymbol(), "charge": charge})
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
        # The bridge is built on the Qt main thread; remembered so call() can
        # tell when it is already there.
        self._owner_thread = threading.get_ident()
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
        if threading.get_ident() == self._owner_thread:
            # Already on the main thread: a queued signal could only be
            # delivered once this call returned, so waiting for it would
            # freeze the UI for the whole timeout and then fail.
            return execute_operation(self._context, operation, dict(args))
        container: Dict[str, Any] = {
            "event": threading.Event(),
            "lock": threading.Lock(),
            "state": "queued",
            "result": None,
            "error": None,
        }
        self._request.emit(operation, args, container)
        if not container["event"].wait(timeout):
            with container["lock"]:
                if container["state"] == "queued":
                    # Never started: withdraw it, so a busy main thread does
                    # not later apply an edit the client was told had failed.
                    container["state"] = "cancelled"
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
        with c["lock"]:
            if c["state"] == "cancelled":
                logger.warning("Skipping %r: the caller already timed out", operation)
                return
            c["state"] = "running"
        try:
            c["result"] = execute_operation(
                self._context, operation, dict(args)  # type: ignore[arg-type]
            )
        except Exception as exc:  # pylint: disable=broad-except
            c["error"] = exc
        finally:
            c["event"].set()

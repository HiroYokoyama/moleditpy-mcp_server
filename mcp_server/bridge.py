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

from PyQt6.QtCore import QObject, Qt, pyqtSignal

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
        bond_colors = args.get("bond_colors")
        if not bond_colors:
            raise ValueError("'bond_colors' argument is required")
        ctrl = ctx.get_3d_controller()
        if ctrl is None:
            raise ValueError("3D controller is not available (is the 3D viewer active?)")
        for idx_str, color in bond_colors.items():
            ctrl.set_bond_color(int(idx_str), color)
        ctx.refresh_3d_view()
        return {"success": True}

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

    if operation == "get_file_io_config":
        return _get_file_io_config(ctx)

    if operation == "set_file_io_config":
        return _set_file_io_config(ctx, args)

    raise ValueError(f"Unknown operation: {operation!r}")


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

    reactant = Chem.AddHs(mol)
    products = rxn.RunReactants((reactant,))
    if not products:
        reactant = mol
        products = rxn.RunReactants((reactant,))
    if not products:
        raise ValueError(
            "The reaction pattern did not match the current molecule. "
            "Check the SMARTS (explicit [H] atoms are available for matching)."
        )

    selected = _select_product_by_anchor(rxn, reactant, products, args.get("atom_index"))
    new_mol = products[selected][0]

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

    clean_mol = Chem.MolFromSmiles(Chem.MolToSmiles(new_mol))
    if clean_mol is None:
        raise ValueError(
            "Transformation produced an invalid molecule (failed sanitization). "
            "Refine the reaction SMARTS."
        )

    orig_count = mol.GetNumAtoms()
    new_count = clean_mol.GetNumAtoms()
    if orig_count > 5 and new_count < orig_count * 0.7:
        raise ValueError(
            f"Safety guard: transformation caused massive atom loss "
            f"({orig_count} -> {new_count} heavy atoms). Aborted."
        )

    final_smiles = Chem.MolToSmiles(clean_mol)
    ctx.load_from_smiles(final_smiles)
    ctx.push_undo_checkpoint()
    ctx.refresh_ui()
    return {
        "success": True,
        "smiles": final_smiles,
        "num_products": len(products),
        "selected_product": selected,
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


def _get_app_info(ctx: Any) -> Dict[str, Any]:
    from mcp_server import PLUGIN_VERSION  # pylint: disable=import-outside-toplevel
    mw = ctx.get_main_window()
    version = "unknown"
    if mw is not None:
        version = getattr(mw, "VERSION", None) or (
            getattr(mw.init_manager, "settings", {}).get("app_version", "unknown")
            if hasattr(mw, "init_manager")
            else "unknown"
        )
    return {
        "app": "MoleditPy",
        "version": version,
        "mcp_plugin_version": PLUGIN_VERSION,
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

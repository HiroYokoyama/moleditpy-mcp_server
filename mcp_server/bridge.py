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
        mol_block = args.get("mol_block", "").strip()
        if not mol_block:
            raise ValueError("'mol_block' argument is required")
        mol = ctx.load_from_mol_block(mol_block)
        return {"success": mol is not None}

    if operation == "trigger_3d_conversion":
        ctx.generate_3d_coords()
        return {"success": True}

    if operation == "highlight_atoms":
        atom_colors = args.get("atom_colors")
        if not atom_colors:
            raise ValueError("'atom_colors' argument is required")
        ctx.set_atom_colors(atom_colors)
        return {"success": True}

    if operation == "get_selected_atoms":
        return _get_selected_atoms(ctx)

    if operation == "clear_canvas":
        ctx.clear_canvas(push_to_undo=True)
        return {"success": True}

    if operation == "get_app_info":
        return _get_app_info(ctx)

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

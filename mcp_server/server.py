#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP HTTP server — implements the MCP Streamable HTTP transport.

Runs in a daemon thread. Tool calls are forwarded to the Qt main thread
via the MCPBridge passed at construction time.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import threading
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2024-11-05"

_TOOLS: List[Dict[str, Any]] = [
    # ------------------------------------------------------------------
    # Read molecule state
    # ------------------------------------------------------------------
    {
        "name": "get_current_molecule",
        "description": (
            "Get information about the molecule currently loaded in MoleditPy. "
            "Returns the SMILES string, molecular formula, molecular weight (g/mol), "
            "atom count, bond count, and whether 3D coordinates are available."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_molecule_xyz",
        "description": (
            "Get the 3D XYZ coordinates of the current molecule as a coordinate block. "
            "Each line has the format: Element X Y Z. "
            "Returns an error if no 3D coordinates are available."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_atom_properties",
        "description": (
            "Get detailed properties for one or more atoms by their RDKit indices. "
            "Returns element symbol, atomic number, formal charge, hybridization, "
            "number of explicit/implicit Hs, and number of radical electrons for each atom. "
            "Pass atom_indices as a list of integers, or omit it (or pass an empty list) "
            "to get properties for all atoms."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "atom_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "List of 0-based RDKit atom indices. "
                        "Omit or pass [] to query all atoms."
                    ),
                }
            },
        },
    },
    {
        "name": "get_bond_info",
        "description": (
            "Get the bond table of the current molecule. "
            "For each bond returns: bond index, atom indices of both endpoints, "
            "and bond type (SINGLE, DOUBLE, TRIPLE, AROMATIC)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_selected_atoms",
        "description": (
            "Get the atoms currently selected by the user "
            "in the MoleditPy 2D or 3D view. "
            "Returns atom indices and element symbols."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ------------------------------------------------------------------
    # Load / modify molecule
    # ------------------------------------------------------------------
    {
        "name": "load_molecule_from_smiles",
        "description": (
            "Load a molecule into the MoleditPy 2D editor from a SMILES string. "
            "The molecule is drawn on the 2D canvas immediately."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "smiles": {
                    "type": "string",
                    "description": "The SMILES string of the molecule to load.",
                }
            },
            "required": ["smiles"],
        },
    },
    {
        "name": "load_from_mol_block",
        "description": (
            "Load a molecule from a MOL/SDF block (multi-line text in V2000 or V3000 format). "
            "The molecule replaces the current canvas content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mol_block": {
                    "type": "string",
                    "description": "The MOL or SDF block text.",
                }
            },
            "required": ["mol_block"],
        },
    },
    {
        "name": "show_xyz_in_viewer",
        "description": (
            "Display XYZ coordinate data in the MoleditPy 3D viewer. "
            "Each line of xyz_text must have the format: Element X Y Z. "
            "Standard XYZ file headers (atom count, comment line) are also accepted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "xyz_text": {
                    "type": "string",
                    "description": (
                        "XYZ coordinate data. One atom per line: 'Element X Y Z'. "
                        "Standard XYZ file headers are accepted."
                    ),
                },
                "source_name": {
                    "type": "string",
                    "description": (
                        "Optional label shown in the status bar "
                        "(e.g. 'ORCA result', 'optimized geometry')."
                    ),
                },
            },
            "required": ["xyz_text"],
        },
    },
    {
        "name": "trigger_3d_conversion",
        "description": (
            "Trigger MoleditPy's 2D-to-3D coordinate generation on the current molecule. "
            "This runs the built-in 3D optimizer (ETKDG / MMFF) and switches the view "
            "to the 3D panel. Call get_molecule_xyz afterwards to retrieve the coordinates."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ------------------------------------------------------------------
    # Visual / 3D
    # ------------------------------------------------------------------
    {
        "name": "highlight_atoms",
        "description": (
            "Override the display color of specific atoms in the 3D viewer. "
            "Useful for visually emphasizing active sites, selected atoms, or "
            "computed results. Colors persist until the next full redraw."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "atom_colors": {
                    "type": "object",
                    "description": (
                        "Mapping of atom index (as string key) to hex color string "
                        "(e.g. {\"0\": \"#FF0000\", \"3\": \"#00FF00\"})."
                    ),
                    "additionalProperties": {"type": "string"},
                }
            },
            "required": ["atom_colors"],
        },
    },
    # ------------------------------------------------------------------
    # Canvas / utility
    # ------------------------------------------------------------------
    {
        "name": "clear_canvas",
        "description": (
            "Clear the MoleditPy 2D editor canvas, removing all atoms and bonds. "
            "An undo checkpoint is saved before clearing so the action is reversible."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_app_info",
        "description": (
            "Get information about the running MoleditPy application "
            "and this MCP server plugin."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ------------------------------------------------------------------
    # Load by name (PubChem lookup)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Python execution (runs on Qt main thread, has full ctx access)
    # ------------------------------------------------------------------
    {
        "name": "run_python",
        "description": (
            "Execute arbitrary Python code on the Qt main thread with full access to the "
            "MoleditPy PluginContext as `ctx`. "
            "stdout and stderr are captured and returned. "
            "Assign any value to `result` to get it back. "
            "Use this for complex RDKit manipulations, or to read/push molecules directly "
            "back to the editor (e.g. `ctx.current_molecule = mol; ctx.refresh_ui()`). "
            "Example: `result = ctx.current_molecule.GetNumAtoms()`. "
            "The code runs in an isolated namespace — no extra sandbox restrictions, "
            "so limit use to trusted operations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source code to execute (may be multi-line).",
                }
            },
            "required": ["code"],
        },
    },
    {
        "name": "load_molecule_by_name",
        "description": (
            "Look up a molecule by its common name or IUPAC name on PubChem, "
            "retrieve its SMILES, and load it into the MoleditPy 2D editor. "
            "Examples: 'aspirin', 'caffeine', 'water', 'glucose', 'methanol'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Common name or IUPAC name, e.g. 'aspirin' or 'acetylsalicylic acid'.",
                }
            },
            "required": ["name"],
        },
    },
    # ------------------------------------------------------------------
    # 3D / UI helpers
    # ------------------------------------------------------------------
    {
        "name": "push_undo_checkpoint",
        "description": (
            "Push the current molecular state onto the undo stack. "
            "Call this AFTER modifying the molecule so the user can revert. "
            "The system only records a new checkpoint if the state has changed."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "enter_3d_mode",
        "description": (
            "Switch the MoleditPy UI to 3D viewer mode. "
            "Maximizes the 3D scene and minimizes the 2D drawing canvas."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fit_2d_view",
        "description": "Fit all visible items in the 2D editor canvas into the viewport.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "reset_3d_camera",
        "description": "Reset and re-center the 3D camera to fit the current molecule.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_3d_view",
        "description": (
            "Force a lightweight redraw of the 3D scene. "
            "Use after color overrides (highlight_atoms / highlight_bonds) "
            "to make them immediately visible."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_chemistry",
        "description": (
            "Trigger MoleditPy's chemistry validation pass. "
            "Updates valence-violation flags on atoms, visible in the 2D view. "
            "Also refreshes the UI info panel."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_ui",
        "description": (
            "Sync the MoleditPy info panel, undo/redo button states, "
            "and title bar after an edit. "
            "Use after direct molecule modifications that bypass the undo system."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "highlight_bonds",
        "description": (
            "Override the display color of specific bonds in the 3D viewer. "
            "bond_colors maps bond index (as string key) to a hex color "
            "(e.g. {\"0\": \"#FF0000\", \"3\": \"#0000FF\"}). "
            "Call refresh_3d_view afterwards to show the changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bond_colors": {
                    "type": "object",
                    "description": "Bond index → hex color, e.g. {\"0\": \"#FF0000\"}.",
                    "additionalProperties": {"type": "string"},
                }
            },
            "required": ["bond_colors"],
        },
    },
    # ------------------------------------------------------------------
    # Plugin authoring helpers
    # ------------------------------------------------------------------
    {
        "name": "get_plugin_dev_manual",
        "description": (
            "Fetch the MoleditPy Plugin Development Manual (V4) from GitHub. "
            "Read this FIRST before writing any plugin — it contains the full "
            "PluginContext API reference, lifecycle hooks, example code, and "
            "packaging instructions."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_app_source_tree",
        "description": (
            "Return a recursive directory tree of the installed moleditpy package source. "
            "Call this first to orient yourself — it shows every file and subdirectory "
            "with sizes, so you know exactly what paths to pass to get_app_source. "
            "Optionally pass a sub-path (e.g. 'plugins') to tree only that subtree."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Optional sub-path within the package to tree "
                        "(e.g. 'plugins'). Omit for the full package tree."
                    ),
                }
            },
        },
    },
    {
        "name": "get_app_source",
        "description": (
            "Read a source file or list a directory from the installed moleditpy package. "
            "Pass a path relative to the package root "
            "(e.g. 'plugins/plugin_interface.py', 'core/molecular_data.py', or '.'). "
            "Use this to inspect the real API before writing a plugin."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Path relative to the moleditpy package root, e.g. "
                        "'plugins/plugin_interface.py' or '.' for the root listing."
                    ),
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_plugin_dir",
        "description": (
            "Return the absolute path to MoleditPy's plugin directory "
            "('~/.moleditpy/plugins/' on Linux/macOS, "
            "or '%USERPROFILE%\\.moleditpy\\plugins\\' on Windows). "
            "Write new plugin files here, then call reload_plugins."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "reload_plugins",
        "description": (
            "Trigger MoleditPy to re-scan and reload all plugins from the plugin directory. "
            "Call this after writing or updating a plugin via write_text_file. "
            "Returns the number of plugins found."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ------------------------------------------------------------------
    # File I/O (sandboxed to the configured base directory)
    # ------------------------------------------------------------------
    {
        "name": "write_text_file",
        "description": (
            "Write text content to a file inside the configured base directory. "
            "The path is relative to that directory. "
            "Parent subdirectories are created automatically. "
            "Set overwrite=true to replace an existing file (default: false). "
            "Only extensions on the allowed list are accepted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path, e.g. 'run1/molecule.inp'",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write (UTF-8).",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Allow overwriting an existing file (default false).",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_text_file",
        "description": (
            "Read and return the UTF-8 text content of a file inside the "
            "configured base directory. Path is relative to that directory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": (
            "List files and subdirectories at a path inside the base directory. "
            "Omit path (or use '.') to list the base directory itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path (default '.' = base directory).",
                },
            },
        },
    },
    {
        "name": "delete_file",
        "description": (
            "Permanently delete a file inside the base directory. "
            "This action cannot be undone. "
            "You MUST pass confirm=true explicitly to proceed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to authorise the deletion.",
                },
            },
            "required": ["path", "confirm"],
        },
    },
    {
        "name": "get_file_io_config",
        "description": (
            "Get the current file I/O sandbox configuration: "
            "base directory and allowed file extensions."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_file_io_config",
        "description": (
            "Configure the file I/O sandbox. "
            "base_dir must be an existing absolute directory path — "
            "all file tools are restricted to that directory tree. "
            "allowed_extensions is an optional list of permitted extensions "
            "(e.g. ['.inp', '.txt', '.xyz']); omit to keep the current list."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "base_dir": {
                    "type": "string",
                    "description": "Absolute path to the sandbox directory.",
                },
                "allowed_extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of permitted extensions (e.g. ['.inp', '.xyz']).",
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool result helpers
# ---------------------------------------------------------------------------


def _tool_ok(text: str) -> Dict[str, Any]:
    """Return a successful MCP tool result."""
    return {"content": [{"type": "text", "text": text}]}


def _tool_err(text: str) -> Dict[str, Any]:
    """Return a failed MCP tool result."""
    return {"content": [{"type": "text", "text": text}], "isError": True}


# ---------------------------------------------------------------------------
# File I/O sandbox helpers
# ---------------------------------------------------------------------------

_MAX_FILE_BYTES = 4 * 1024 * 1024  # 4 MB hard limit for reads and writes


def _resolve_safe_path(user_path: str, base_dir: str) -> Path:
    """
    Resolve *user_path* relative to *base_dir* and verify it stays inside.

    Raises ValueError on path traversal or absolute user_path.
    """
    if Path(user_path).is_absolute():
        raise ValueError(
            "Absolute paths are not accepted. Use a path relative to the base directory."
        )
    base = Path(base_dir).expanduser().resolve()
    resolved = (base / user_path).resolve()
    # Ensure the resolved path is inside the base (strict prefix check)
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError(
            f"Path {user_path!r} resolves outside the allowed directory."
        )
    return resolved


def _check_extension(path: Path, allowed_extensions: List[str]) -> None:
    """Raise ValueError if path's extension is not in *allowed_extensions*."""
    ext = path.suffix.lower()
    if not ext:
        raise ValueError(
            f"{path.name!r} has no extension. "
            f"Allowed extensions: {', '.join(sorted(allowed_extensions))}"
        )
    if ext not in {e.lower() for e in allowed_extensions}:
        raise ValueError(
            f"Extension {ext!r} is not on the allowlist. "
            f"Allowed: {', '.join(sorted(allowed_extensions))}\n"
            "Use set_file_io_config to add it."
        )


def _get_sandbox(bridge: Any) -> tuple[str, List[str]]:
    """
    Fetch the current file I/O config from the bridge.

    Returns (base_dir, allowed_extensions).
    Raises ValueError if base_dir is not configured.
    """
    cfg = bridge.call("get_file_io_config")
    base_dir: Optional[str] = cfg.get("base_dir")
    if not base_dir:
        raise ValueError(
            "File I/O base directory is not configured. "
            "Call set_file_io_config with a base_dir first."
        )
    allowed: List[str] = cfg.get("allowed_extensions", [])
    return base_dir, allowed


# ---------------------------------------------------------------------------
# PubChem helper (runs in server thread — no Qt needed)
# ---------------------------------------------------------------------------


_PLUGIN_DEV_MANUAL_URL = (
    "https://hiroyokoyama.github.io/python_molecular_editor/docs/PLUGIN_DEVELOPMENT_MANUAL_V4.md"
)


def _fetch_plugin_dev_manual() -> str:
    """Fetch the plugin development manual from GitHub. Raises ValueError on failure."""
    try:
        with urllib.request.urlopen(_PLUGIN_DEV_MANUAL_URL, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"Failed to fetch plugin development manual (HTTP {exc.code}). "
            "Check your internet connection or try again."
        ) from exc
    except Exception as exc:
        raise ValueError(f"Failed to fetch plugin development manual: {exc}") from exc


def _fetch_smiles_by_name(name: str) -> str:
    """
    Resolve *name* to an (isomeric) SMILES string via the PubChem REST API.

    PubChem's 2025 PUG-REST update renamed the ``IsomericSMILES`` property
    to ``SMILES`` (both in the request and the response JSON), so we request
    ``SMILES`` and accept either key in the response.

    Raises ``ValueError`` if the compound is not found or the request fails.
    """
    url = (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
        + urllib.parse.quote(name)
        + "/property/SMILES/JSON"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        props = data["PropertyTable"]["Properties"][0]
        smiles = props.get("SMILES") or props.get("IsomericSMILES")
        if not smiles:
            raise ValueError(
                f"PubChem returned no SMILES for {name!r} "
                f"(available properties: {sorted(props)})"
            )
        return smiles
    except ValueError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError(
                f"Compound {name!r} was not found on PubChem. "
                "Try a different name, IUPAC name, or CAS number."
            ) from exc
        raise ValueError(f"PubChem request failed (HTTP {exc.code})") from exc
    except Exception as exc:
        raise ValueError(f"PubChem lookup error: {exc}") from exc


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def dispatch_tool(  # noqa: C901
    bridge: Any,
    name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Dispatch a named MCP tool call through *bridge* and return the result dict.

    All calls to *bridge.call()* block until the Qt main thread processes them.
    """
    try:
        if name == "get_current_molecule":
            info = bridge.call("get_molecule_info")
            if not info["loaded"]:
                return _tool_ok("No molecule is currently loaded in MoleditPy.")
            return _tool_ok(
                f"SMILES: {info['smiles']}\n"
                f"Formula: {info['formula']}\n"
                f"Molecular Weight: {info['molecular_weight']:.4f} g/mol\n"
                f"Atoms: {info['num_atoms']}\n"
                f"Bonds: {info['num_bonds']}\n"
                f"3D coordinates: "
                f"{'available' if info['has_3d_coords'] else 'not available'}"
            )

        if name == "get_molecule_xyz":
            data = bridge.call("get_xyz_block")
            if not data["has_data"]:
                return _tool_ok(
                    "No 3D coordinates available. "
                    "Use trigger_3d_conversion first, or load XYZ data via show_xyz_in_viewer."
                )
            return _tool_ok(data["xyz_block"])

        if name == "get_atom_properties":
            indices = arguments.get("atom_indices") or []
            data = bridge.call("get_atom_properties", {"atom_indices": indices})
            if not data["atoms"]:
                return _tool_ok("No molecule loaded or no atoms found.")
            lines = [f"Atom properties ({len(data['atoms'])} atom(s)):"]
            for a in data["atoms"]:
                lines.append(
                    f"  [{a['index']}] {a['symbol']} "
                    f"Z={a['atomic_num']} "
                    f"charge={a['formal_charge']} "
                    f"hybridization={a['hybridization']} "
                    f"nHs={a['total_hs']} "
                    f"radical_e={a['num_radical_electrons']}"
                )
            return _tool_ok("\n".join(lines))

        if name == "get_bond_info":
            data = bridge.call("get_bond_info")
            if not data["bonds"]:
                return _tool_ok(
                    "No molecule loaded or molecule has no bonds."
                )
            lines = [f"Bond table ({len(data['bonds'])} bond(s)):"]
            for b in data["bonds"]:
                lines.append(
                    f"  bond {b['index']}: "
                    f"atom {b['atom1']} — atom {b['atom2']}  {b['bond_type']}"
                )
            return _tool_ok("\n".join(lines))

        if name == "get_selected_atoms":
            data = bridge.call("get_selected_atoms")
            if data["count"] == 0:
                return _tool_ok("No atoms are currently selected.")
            lines = [f"Selected {data['count']} atom(s):"]
            for atom in data["selected_atoms"]:
                lines.append(
                    f"  Index {atom['index']}: {atom['symbol']} "
                    f"(Z={atom['atomic_num']})"
                )
            return _tool_ok("\n".join(lines))

        if name == "load_molecule_from_smiles":
            smiles = arguments.get("smiles", "").strip()
            if not smiles:
                return _tool_err("'smiles' argument is required.")
            bridge.call("load_smiles", {"smiles": smiles})
            return _tool_ok(f"Molecule loaded from SMILES: {smiles}")

        if name == "load_from_mol_block":
            mol_block = arguments.get("mol_block", "").strip()
            if not mol_block:
                return _tool_err("'mol_block' argument is required.")
            result = bridge.call("load_mol_block", {"mol_block": mol_block})
            if result["success"]:
                return _tool_ok("Molecule loaded from MOL block.")
            return _tool_err("Failed to parse MOL block. Check the format.")

        if name == "show_xyz_in_viewer":
            xyz_text = arguments.get("xyz_text", "").strip()
            source_name = arguments.get("source_name", "MCP input")
            if not xyz_text:
                return _tool_err("'xyz_text' argument is required.")
            result = bridge.call(
                "show_xyz",
                {"xyz_text": xyz_text, "source_name": source_name},
            )
            if result["success"]:
                return _tool_ok(
                    f"XYZ data displayed in 3D viewer (source: {source_name})."
                )
            return _tool_err("Failed to parse XYZ data. Verify the format.")

        if name == "trigger_3d_conversion":
            bridge.call("trigger_3d_conversion")
            return _tool_ok(
                "3D conversion triggered. "
                "Use get_molecule_xyz to retrieve the generated coordinates."
            )

        if name == "highlight_atoms":
            atom_colors = arguments.get("atom_colors")
            if not atom_colors:
                return _tool_err("'atom_colors' argument is required.")
            bridge.call("highlight_atoms", {"atom_colors": atom_colors})
            return _tool_ok(
                f"Highlighted {len(atom_colors)} atom(s) in the 3D viewer."
            )

        if name == "clear_canvas":
            bridge.call("clear_canvas")
            return _tool_ok("Canvas cleared.")

        if name == "get_app_info":
            info = bridge.call("get_app_info")
            return _tool_ok(
                f"Application: {info['app']}\n"
                f"Version: {info['version']}\n"
                f"MCP Plugin: {info['mcp_plugin_version']}"
            )

        if name == "run_python":
            code = arguments.get("code", "").strip()
            if not code:
                return _tool_err("'code' argument is required.")
            result = bridge.call("run_python", {"code": code}, timeout=30.0)
            parts: List[str] = []
            if result.get("stdout"):
                parts.append(f"stdout:\n{result['stdout']}")
            if result.get("stderr"):
                parts.append(f"stderr:\n{result['stderr']}")
            res_repr = result.get("result", "None")
            if res_repr != "None":
                parts.append(f"result: {res_repr}")
            return _tool_ok("\n".join(parts) or "(no output)")

        if name == "load_molecule_by_name":
            mol_name = arguments.get("name", "").strip()
            if not mol_name:
                return _tool_err("'name' argument is required.")
            smiles = _fetch_smiles_by_name(mol_name)
            bridge.call("load_smiles", {"smiles": smiles})
            return _tool_ok(
                f"Loaded {mol_name!r} from PubChem.\nSMILES: {smiles}"
            )

        if name == "push_undo_checkpoint":
            bridge.call("push_undo_checkpoint")
            return _tool_ok("Undo checkpoint pushed.")

        if name == "enter_3d_mode":
            bridge.call("enter_3d_mode")
            return _tool_ok("Switched to 3D viewer mode.")

        if name == "fit_2d_view":
            bridge.call("fit_2d_view")
            return _tool_ok("2D canvas fitted to molecule.")

        if name == "reset_3d_camera":
            bridge.call("reset_3d_camera")
            return _tool_ok("3D camera reset.")

        if name == "refresh_3d_view":
            bridge.call("refresh_3d_view")
            return _tool_ok("3D view refreshed.")

        if name == "check_chemistry":
            bridge.call("check_chemistry")
            return _tool_ok("Chemistry validation complete.")

        if name == "refresh_ui":
            bridge.call("refresh_ui")
            return _tool_ok("UI refreshed.")

        if name == "highlight_bonds":
            bond_colors = arguments.get("bond_colors")
            if not bond_colors:
                return _tool_err("'bond_colors' argument is required.")
            bridge.call("highlight_bonds", {"bond_colors": bond_colors})
            return _tool_ok(
                f"Highlighted {len(bond_colors)} bond(s) in the 3D viewer."
            )

        # ------------------------------------------------------------------
        # Plugin authoring helpers (run in server thread — no Qt for fetch/read)
        # ------------------------------------------------------------------

        if name == "get_plugin_dev_manual":
            manual = _fetch_plugin_dev_manual()
            return _tool_ok(manual)

        if name == "list_app_source_tree":
            path = arguments.get("path", "").strip()
            result = bridge.call("list_app_source_tree", {"path": path})
            return _tool_ok(result["content"])

        if name == "get_app_source":
            path = arguments.get("path", "").strip()
            if not path:
                return _tool_err("'path' argument is required.")
            result = bridge.call("get_app_source", {"path": path})
            return _tool_ok(result["content"])

        if name == "get_plugin_dir":
            result = bridge.call("get_plugin_dir")
            return _tool_ok(
                f"Plugin directory: {result['plugin_dir']}\n"
                "Write new plugin files here, then call reload_plugins."
            )

        if name == "reload_plugins":
            result = bridge.call("reload_plugins")
            return _tool_ok(
                f"Plugins reloaded. {result['plugin_count']} plugin(s) found."
            )

        # ------------------------------------------------------------------
        # File I/O tools (sandboxed; run in server thread — no Qt needed)
        # ------------------------------------------------------------------

        if name == "get_file_io_config":
            cfg = bridge.call("get_file_io_config")
            base_dir = cfg.get("base_dir") or "(not configured)"
            exts = ", ".join(cfg.get("allowed_extensions", []))
            return _tool_ok(
                f"Base directory: {base_dir}\n"
                f"Allowed extensions: {exts or '(none)'}"
            )

        if name == "set_file_io_config":
            args_inner: Dict[str, Any] = {}
            if "base_dir" in arguments:
                bd = arguments["base_dir"]
                p = Path(bd).expanduser().resolve()
                if not p.is_dir():
                    return _tool_err(
                        f"{bd!r} does not exist or is not a directory. "
                        "Create it first or provide an existing path."
                    )
                args_inner["base_dir"] = str(p)
            if "allowed_extensions" in arguments:
                args_inner["allowed_extensions"] = arguments["allowed_extensions"]
            if not args_inner:
                return _tool_err("Provide at least base_dir or allowed_extensions.")
            bridge.call("set_file_io_config", args_inner)
            parts = []
            if "base_dir" in args_inner:
                parts.append(f"Base directory: {args_inner['base_dir']}")
            if "allowed_extensions" in args_inner:
                parts.append(f"Allowed extensions: {', '.join(args_inner['allowed_extensions'])}")
            return _tool_ok("File I/O config updated.\n" + "\n".join(parts))

        if name == "write_text_file":
            user_path = arguments.get("path", "").strip()
            content = arguments.get("content", "")
            overwrite = bool(arguments.get("overwrite", False))
            if not user_path:
                return _tool_err("'path' argument is required.")
            if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
                return _tool_err(
                    f"Content exceeds the {_MAX_FILE_BYTES // 1024 // 1024} MB limit."
                )
            base_dir, allowed_exts = _get_sandbox(bridge)
            target = _resolve_safe_path(user_path, base_dir)
            _check_extension(target, allowed_exts)
            if target.exists() and not overwrite:
                return _tool_err(
                    f"{user_path!r} already exists. Pass overwrite=true to replace it."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            size = target.stat().st_size
            return _tool_ok(
                f"Written: {user_path} ({size:,} bytes)"
            )

        if name == "read_text_file":
            user_path = arguments.get("path", "").strip()
            if not user_path:
                return _tool_err("'path' argument is required.")
            base_dir, allowed_exts = _get_sandbox(bridge)
            target = _resolve_safe_path(user_path, base_dir)
            _check_extension(target, allowed_exts)
            if not target.exists():
                return _tool_err(f"{user_path!r} does not exist.")
            if not target.is_file():
                return _tool_err(f"{user_path!r} is not a file.")
            size = target.stat().st_size
            if size > _MAX_FILE_BYTES:
                return _tool_err(
                    f"File is {size:,} bytes, exceeding the "
                    f"{_MAX_FILE_BYTES // 1024 // 1024} MB read limit."
                )
            return _tool_ok(target.read_text(encoding="utf-8"))

        if name == "list_directory":
            user_path = arguments.get("path", ".") or "."
            base_dir, _ = _get_sandbox(bridge)
            target = _resolve_safe_path(user_path, base_dir)
            if not target.exists():
                return _tool_err(f"{user_path!r} does not exist.")
            if not target.is_dir():
                return _tool_err(f"{user_path!r} is not a directory.")
            entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
            dirs = [e for e in entries if e.is_dir()]
            files = [e for e in entries if e.is_file()]
            lines = [f"Directory: {target}"]
            if dirs:
                lines.append("Subdirectories:")
                for d in dirs:
                    lines.append(f"  {d.name}/")
            if files:
                lines.append("Files:")
                for f in files:
                    lines.append(f"  {f.name}  ({f.stat().st_size:,} bytes)")
            if not dirs and not files:
                lines.append("(empty)")
            return _tool_ok("\n".join(lines))

        if name == "delete_file":
            user_path = arguments.get("path", "").strip()
            confirm = arguments.get("confirm", False)
            if not user_path:
                return _tool_err("'path' argument is required.")
            if not confirm:
                return _tool_err(
                    "Deletion is irreversible. Pass confirm=true to proceed."
                )
            base_dir, allowed_exts = _get_sandbox(bridge)
            target = _resolve_safe_path(user_path, base_dir)
            _check_extension(target, allowed_exts)
            if not target.exists():
                return _tool_err(f"{user_path!r} does not exist.")
            if not target.is_file():
                return _tool_err(f"{user_path!r} is not a regular file.")
            target.unlink()
            return _tool_ok(f"Deleted: {user_path}")

        return _tool_err(f"Unknown tool: {name!r}")

    except TimeoutError:
        return _tool_err(
            "Timed out waiting for MoleditPy to respond. "
            "The application may be busy."
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Tool %r raised an unhandled exception", name)
        return _tool_err(f"Error: {exc}")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class _MCPHandler(BaseHTTPRequestHandler):
    """HTTP request handler implementing MCP Streamable HTTP transport."""

    # Set by MCPHttpServer before starting — shared class-level references.
    bridge: Any = None
    server_name: str = "MoleditPy MCP Server"
    server_version: str = "unknown"
    session_id: str = ""

    def log_message(self, format_str: str, *args: Any) -> None:  # type: ignore[override]
        logger.debug(format_str, *args)

    # ------------------------------------------------------------------
    # CORS helpers
    # ------------------------------------------------------------------

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Accept, Mcp-Session-Id",
        )
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # pylint: disable=invalid-name
        self.send_response(200)
        self._send_cors()
        self.end_headers()

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        if self.path in ("/", "/health"):
            body = json.dumps(
                {"status": "ok", "server": type(self).server_name}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._send_cors()
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        if self.path != "/mcp":
            self.send_error(404, "Use POST /mcp")
            return
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self.send_error(400, "Empty body")
            return
        try:
            raw = self.rfile.read(length)
            message = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            self._send_json(
                {"jsonrpc": "2.0", "error": {"code": -32700, "message": str(exc)}, "id": None}
            )
            return
        self._process(message)

    # ------------------------------------------------------------------
    # MCP JSON-RPC processing
    # ------------------------------------------------------------------

    def _process(self, message: Dict[str, Any]) -> None:
        msg_id = message.get("id")
        method = message.get("method", "")
        params: Dict[str, Any] = message.get("params") or {}

        # Notifications (no id) — acknowledge with 202
        if msg_id is None:
            logger.debug("MCP notification: %s", method)
            self.send_response(202)
            self._send_cors()
            self.end_headers()
            return

        try:
            result = self._handle_method(method, params)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Unhandled error processing %r", method)
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": f"Internal error: {exc}"},
                    "id": msg_id,
                }
            )
            return

        self._send_json({"jsonrpc": "2.0", "result": result, "id": msg_id})

    def _handle_method(self, method: str, params: Dict[str, Any]) -> Any:
        cls = type(self)
        if method == "initialize":
            return {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": cls.server_name,
                    "version": cls.server_version,
                },
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": _TOOLS}
        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments: Dict[str, Any] = params.get("arguments") or {}
            if cls.bridge is None:
                return _tool_err("Bridge not initialized.")
            return dispatch_tool(cls.bridge, tool_name, arguments)
        raise _MethodNotFound(method)

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _send_json(self, data: Dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Mcp-Session-Id", type(self).session_id)
        self._send_cors()
        self.end_headers()
        self.wfile.write(body)


class _MethodNotFound(Exception):
    """Raised when an unknown JSON-RPC method is requested."""


# ---------------------------------------------------------------------------
# Threaded HTTP server wrapper
# ---------------------------------------------------------------------------


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MCPHttpServer:
    """Manages the lifecycle of the background MCP HTTP server thread."""

    def __init__(
        self,
        bridge: Any,
        server_name: str,
        server_version: str,
        host: str = "127.0.0.1",
        port: int = 7891,
    ) -> None:
        self._bridge = bridge
        self._server_name = server_name
        self._server_version = server_version
        self._host = host
        self._port = port
        self._httpd: Optional[_ThreadedHTTPServer] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the HTTP server in a daemon thread."""
        _MCPHandler.bridge = self._bridge
        _MCPHandler.server_name = self._server_name
        _MCPHandler.server_version = self._server_version
        _MCPHandler.session_id = uuid.uuid4().hex
        self._httpd = _ThreadedHTTPServer((self._host, self._port), _MCPHandler)
        t = threading.Thread(
            target=self._httpd.serve_forever,
            name="mcp-http-server",
            daemon=True,
        )
        t.start()
        logger.info("MCP server listening at http://%s:%d/mcp", self._host, self._port)

    def stop(self) -> None:
        """Stop the HTTP server."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd = None
            logger.info("MCP server stopped")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/mcp"

    @property
    def port(self) -> int:
        return self._port

#!/usr/bin/env python3
"""
MCP HTTP server — implements the MCP Streamable HTTP transport.

Runs in a daemon thread. Tool calls are forwarded to the Qt main thread
via the MCPBridge passed at construction time.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import logging
import os
import re
import socket
import socketserver
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# --- Protocol versions -----------------------------------------------------
# Two eras of MCP are spoken here:
#   * "legacy"  — handshake-based (`initialize`), 2025-11-25 and earlier.
#   * "modern"  — stateless per-request metadata, 2026-07-28 and later.
_PROTOCOL_VERSION = "2024-11-05"  # legacy default when none requested
_MODERN_PROTOCOL_VERSION = "2026-07-28"
_LEGACY_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
_MODERN_PROTOCOL_VERSIONS = (_MODERN_PROTOCOL_VERSION,)

#: Valid values for the ``protocol_mode`` setting.
PROTOCOL_MODES = ("auto", "legacy", "modern")

# `_meta` keys defined by the 2026-07-28 revision.
_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
_META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
_META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# JSON-RPC error codes (2026-07-28 protocol-defined sub-range).
_ERR_METHOD_NOT_FOUND = -32601
_ERR_HEADER_MISMATCH = -32020
_ERR_UNSUPPORTED_PROTOCOL_VERSION = -32022

# Cache hints returned on CacheableResult responses (2026-07-28).
_TOOLS_TTL_MS = 300_000  # tool list only changes when the plugin is updated
_DISCOVER_TTL_MS = 300_000

# Every 2026-07-28 result must say whether it is final. This server never
# returns partial or input-requesting results, so it is always "complete".
_RESULT_TYPE_COMPLETE = "complete"

#: Natural-language guidance returned by `server/discover` (2026-07-28) so the
#: client can prime its model with what this server is for.
_SERVER_INSTRUCTIONS = (
    "This server drives a running instance of MoleditPy, a desktop molecular "
    "editor for preparing quantum-chemistry calculations. The user is watching "
    "the same window you are editing.\n"
    "\n"
    "Typical flow: read the current state (get_current_molecule, "
    "get_molecule_xyz, get_bond_info), modify it (load_molecule_from_smiles, "
    "load_molecule_by_name, apply_reaction_smarts), then generate input files "
    "with write_file_with_xyz_block — never retype coordinates by hand.\n"
    "\n"
    "Before a destructive edit, call push_undo_checkpoint so the user can undo. "
    "3D coordinates only exist after trigger_3d_conversion. File tools are "
    "confined to the base directory configured in the plugin's settings "
    "dialog; grep_files and find_files search that directory, the installed "
    "MoleditPy source, or the user's plugin folder — use them (plus "
    "get_plugin_dev_manual and get_app_source) when writing MoleditPy plugins.\n"
    "\n"
    "XYZ data (show_xyz_in_viewer, load_xyz_file) never opens the app's charge "
    "dialog over MCP: pass 'charge' for ions, or skip_chemistry=true to keep "
    "distance-based bonds only. For figures, fix the view with set_3d_camera "
    "(atom-based directions, parallel projection), choose set_3d_style, add "
    "long contacts with edit_bonds, and write it with "
    "save_molecule_image (background may be 'transparent').\n"
    "\n"
    "Widening file access always needs the user's approval in a MoleditPy "
    "dialog: request_read_folder asks for a read-only folder, and "
    "set_file_io_config asks before changing the base directory or the "
    "extension list. If the user declines, do not retry the same request."
)

#: Background option shared by the two image tools.
_IMAGE_BACKGROUND_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "Background color for this capture ('white', '#ffffff', any color "
        "name) or 'transparent' for a PNG with alpha. Default: the viewer's "
        "own background (3D) / white (2D). The viewer is left unchanged."
    ),
}

#: Path wording shared by the read tools.
_READ_PATH_NOTE = (
    "Relative to the sandbox base directory, or an absolute path inside a "
    "read-only folder the user approved (see request_read_folder)."
)

#: Arguments shared by the two XYZ-loading tools.
_XYZ_LOAD_PROPERTIES: dict[str, Any] = {
    "charge": {
        "type": "integer",
        "description": (
            "Total molecular charge for bond-order perception. Without it the "
            "app tries 0 and, if that fails, falls back to distance-based "
            "bonds instead of opening its charge dialog. Pass it for ions: "
            "charge 0 can 'succeed' with wrong bond orders."
        ),
    },
    "skip_chemistry": {
        "type": "boolean",
        "description": (
            "Skip bond-order perception and connect atoms by distance only "
            "(the dialog's 'Skip chemistry'). Good for unusual bonding, "
            "transition states and non-covalent contacts. Default false."
        ),
    },
    "frame": {
        "type": "integer",
        "description": (
            "For multi-frame XYZ (trajectories): 0-based frame, negative "
            "counts from the end. Default: the last frame."
        ),
    },
    "keep_camera": {
        "type": "boolean",
        "description": (
            "Keep the current 3D viewpoint instead of re-framing (for "
            "stepping through frames or comparing similar structures)."
        ),
    },
}

_TOOLS: list[dict[str, Any]] = [
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
            "Get the atoms currently selected by the user in the MoleditPy "
            "2D editor. Returns RDKit atom indices and element symbols. "
            "NOTE: only the 2D canvas selection is captured — the 3D viewer "
            "uses custom picking that is not exposed; ask the user to select "
            "atoms in the 2D editor. An empty result can also mean the "
            "molecule has not been converted to 3D yet (the index mapping "
            "needs it): run trigger_3d_conversion first."
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
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": (
                        "The MOL or SDF block text (multi-line string). "
                        "An array of lines is also accepted if your client "
                        "escapes newlines."
                    ),
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
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": (
                        "XYZ coordinate data. One atom per line: 'Element X Y Z'. "
                        "Standard XYZ file headers are accepted. Multi-line "
                        "string, or an array of lines if your client escapes "
                        "newlines."
                    ),
                },
                "source_name": {
                    "type": "string",
                    "description": (
                        "Optional label shown in the status bar "
                        "(e.g. 'ORCA result', 'optimized geometry')."
                    ),
                },
                **_XYZ_LOAD_PROPERTIES,
            },
            "required": ["xyz_text"],
        },
    },
    {
        "name": "load_xyz_file",
        "description": (
            "Load an .xyz file from the file I/O sandbox into the 3D viewer, "
            "without pasting coordinates. Multi-frame files (optimization "
            "trajectories) are supported: 'frame' picks one (default: the "
            "last); step through a trajectory by calling again with "
            "keep_camera=true. Same charge / skip_chemistry handling as "
            "show_xyz_in_viewer, so no dialog blocks the call."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": _READ_PATH_NOTE,
                },
                **_XYZ_LOAD_PROPERTIES,
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_mapped_smiles",
        "description": (
            "Get the current molecule's SMILES with every atom's RDKit index "
            "embedded as an atom map number, plus an index legend. "
            "IMPORTANT: map number = atom_index + 1 (RDKit reserves map 0 for "
            "'unmapped'), so an atom shown as [c:5] has atom_index 4. "
            "Call this BEFORE apply_reaction_smarts, set_cpk_color_override, "
            "or set_bond_color_override to find out which atom_index refers "
            "to which atom."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "apply_reaction_smarts",
        "description": (
            "Modify the current 2D molecule by applying a Reaction SMARTS "
            "transformation (RDKit RunReactants) and load the product into the editor. "
            "Include atom map numbers for EVERY atom that persists from reactant to "
            "product, e.g. '[c:1][H]>>[c:1][Cl]' for an aromatic chlorination or "
            "'[C:1][H]>>[C:1]O' to add a hydroxyl. Explicit hydrogens are added "
            "before matching, so [H] can be consumed in the pattern. "
            "If the pattern matches several sites, pass atom_index to anchor the "
            "transformation to the intended site. An undo checkpoint is pushed "
            "automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reaction_smarts": {
                    "type": "string",
                    "description": (
                        "Reaction SMARTS 'reactant>>product' with atom map numbers "
                        "on all persisting atoms, e.g. '[c:1][H]>>[c:1][Br]'."
                    ),
                },
                "atom_index": {
                    "type": "integer",
                    "description": (
                        "Optional 0-based RDKit atom index that the matched site "
                        "must contain; disambiguates when the pattern matches "
                        "multiple sites."
                    ),
                },
                "convert_to_3d": {
                    "type": "boolean",
                    "description": (
                        "Run the 2D->3D conversion on the product (default true). "
                        "Keep it enabled when chaining several transformations — "
                        "the active molecule is only updated by the 3D pipeline, "
                        "so without it the next apply_reaction_smarts sees no "
                        "molecule."
                    ),
                },
            },
            "required": ["reaction_smarts"],
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
    {
        "name": "get_molecule_image",
        "description": (
            "Render the current molecule to a PNG image and return it as an "
            "actual image (not text) — the 2D editor canvas or the 3D viewer. "
            "view='auto' (default) picks 3D when the molecule has 3D "
            "coordinates and the 3D viewer is available, otherwise 2D. "
            "Useful for visually checking a structure, a highlight, or a "
            "reaction result without asking the user to look at the screen."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {
                    "type": "string",
                    "enum": ["auto", "2d", "3d"],
                    "description": "Which view to capture (default 'auto').",
                },
                "width": {
                    "type": "integer",
                    "description": "Image width in pixels, 128-2048 (default 900).",
                },
                "height": {
                    "type": "integer",
                    "description": "Image height in pixels, 128-2048 (default 700).",
                },
                "atom_labels": {
                    "type": "boolean",
                    "description": (
                        "3D only: overlay 0-based atom indices for this capture "
                        "(removed again afterwards). Default false."
                    ),
                },
                "background": _IMAGE_BACKGROUND_PROPERTY,
            },
        },
    },
    {
        "name": "save_molecule_image",
        "description": (
            "Render the 2D canvas or 3D viewer to a PNG file inside the file "
            "I/O sandbox (for reports and notes). Set the viewpoint first "
            "with set_3d_camera for a reproducible 3D figure. Only '.png' "
            "paths are accepted, whatever the sandbox extension list says."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Output path relative to the sandbox, ending in .png.",
                },
                "view": {
                    "type": "string",
                    "enum": ["auto", "2d", "3d"],
                    "description": "Which view to capture (default 'auto').",
                },
                "width": {
                    "type": "integer",
                    "description": "Pixels, 128-2048 (default 900).",
                },
                "height": {
                    "type": "integer",
                    "description": "Pixels, 128-2048 (default 700).",
                },
                "atom_labels": {
                    "type": "boolean",
                    "description": "3D only: overlay 0-based atom indices.",
                },
                "background": _IMAGE_BACKGROUND_PROPERTY,
                "overwrite": {
                    "type": "boolean",
                    "description": "Replace an existing file (default false).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_3d_camera",
        "description": (
            "Return the 3D camera as position, focal_point and view_up "
            "(3-vectors). Pass them back to set_3d_camera to restore a view."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_3d_camera",
        "description": (
            "Point the 3D camera explicitly, so figures are reproducible. "
            "Give either 'position' (absolute) or 'direction' (the side you "
            "look FROM, as a vector from the focal point toward the camera, "
            "e.g. [0, 0, 1] looks down the z axis). 'view_up' sets which way "
            "is up on screen. Atom-based alternatives: 'direction_atoms' "
            "[i, j] views from atom j's side along the i->j axis, "
            "'plane_atoms' (3+ atoms) looks straight at their best plane (a "
            "ring face), 'focal_atoms' centers on their centroid. With a "
            "direction the molecule is re-framed ('fit', default true); "
            "'zoom' > 1 then moves in. 'parallel_projection' removes "
            "perspective distortion. Returns the resulting camera."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "position": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "Camera position [x, y, z].",
                },
                "direction": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "View-from direction [x, y, z].",
                },
                "focal_point": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "Point looked at (default: current).",
                },
                "view_up": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3,
                    "maxItems": 3,
                    "description": "Screen-up vector (default: current).",
                },
                "fit": {
                    "type": "boolean",
                    "description": "Re-frame the molecule keeping the orientation.",
                },
                "zoom": {
                    "type": "number",
                    "description": "Zoom factor applied last (>0).",
                },
                "direction_atoms": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": "[i, j]: view from atom j's side along the i->j axis.",
                },
                "plane_atoms": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 3,
                    "description": "Look perpendicular to the best plane of these atoms.",
                },
                "focal_atoms": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                    "description": "Look at the centroid of these atoms.",
                },
                "parallel_projection": {
                    "type": "boolean",
                    "description": "true = orthographic view, false = perspective.",
                },
            },
        },
    },
    {
        "name": "measure_geometry",
        "description": (
            "Measure the current 3D structure by 0-based atom index: 2 "
            "indices give a distance (angstrom), 3 an angle, 4 a dihedral "
            "(degrees). Pass several at once, e.g. [[0, 1], [0, 1, 2], "
            "[0, 1, 2, 3]]."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "atoms": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 4,
                    },
                    "description": "List of index lists (2, 3 or 4 atoms each).",
                },
            },
            "required": ["atoms"],
        },
    },
    {
        "name": "compare_structures",
        "description": (
            "RMSD between the current 3D molecule and another structure with "
            "the same atoms in the same order (e.g. before/after an "
            "optimization, or two levels of theory). The other structure "
            "comes from 'xyz_text' or a sandbox 'path'; trajectories take "
            "'frame' (default last). Kabsch-aligned unless align=false. "
            "overlay=true switches the viewer to the stick style and draws "
            "the other structure over the molecule; the two are told apart "
            "by carbon color (other elements keep CPK colors). "
            "clear_overlay removes it and restores style and colors."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "xyz_text": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "XYZ text of the other structure.",
                },
                "path": {
                    "type": "string",
                    "description": "Or: path of an .xyz file. " + _READ_PATH_NOTE,
                },
                "frame": {
                    "type": "integer",
                    "description": "Frame of a multi-frame XYZ (default -1, the last).",
                },
                "align": {
                    "type": "boolean",
                    "description": "Superimpose first (default true).",
                },
                "heavy_atoms_only": {
                    "type": "boolean",
                    "description": "Ignore hydrogens (default false).",
                },
                "overlay": {
                    "type": "boolean",
                    "description": "Draw the aligned structure in the 3D viewer.",
                },
                "overlay_color": {
                    "type": "string",
                    "description": "Carbon color of the other structure (default '#ff8c00').",
                },
                "current_color": {
                    "type": "string",
                    "description": "Carbon color of the current molecule during the overlay (default '#3fa7d6').",
                },
            },
        },
    },
    {
        "name": "clear_overlay",
        "description": (
            "Remove the overlay drawn by compare_structures and restore the "
            "3D style and atom colors it changed."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ------------------------------------------------------------------
    # Molecule manipulation (direct RDKit access)
    # ------------------------------------------------------------------
    {
        "name": "set_3d_style",
        "description": (
            "Switch the 3D display style: ball_and_stick (app default), cpk "
            "(space-filling), wireframe, or stick. Color overrides are kept."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "style": {
                    "type": "string",
                    "enum": ["ball_and_stick", "cpk", "wireframe", "stick"],
                },
            },
            "required": ["style"],
        },
    },
    {
        "name": "edit_bonds",
        "description": (
            "Add and/or remove bonds on the current molecule by 0-based atom "
            "index, the way the Bond Editor plugin does it. Use it for "
            "contacts that distance-based bonding leaves out (long, partial "
            "or forming bonds) or to drop a wrong one. The "
            "3D coordinates are kept and one undo step is recorded. An edit "
            "that breaks valence rules (an atom above its usual valence) "
            "is kept unsanitized rather than refused, and the result says so."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "add": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "description": "Atom index pairs to bond, e.g. [[0, 12], [5, 12]].",
                },
                "remove": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "description": "Atom index pairs whose bond is removed.",
                },
                "bond_type": {
                    "type": "string",
                    "enum": ["single", "double", "triple", "aromatic"],
                    "description": "Type of the added bonds (default single).",
                },
            },
        },
    },
    {
        "name": "get_molecule_descriptors",
        "description": (
            "Get RDKit's standard descriptor set for the current molecule in one "
            "call: canonical SMILES, formula, molecular weight, exact mass, "
            "LogP (Crippen), TPSA, formal charge, H-bond donor/acceptor counts, "
            "rotatable bond count, ring counts, and atom/bond counts."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_hydrogens",
        "description": (
            "Add explicit hydrogens to the current molecule (RDKit AddHs). "
            "If 3D coordinates are already present, new H positions are "
            "generated along with them. An undo checkpoint is pushed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "explicit_only": {
                    "type": "boolean",
                    "description": (
                        "If true, only add Hs to atoms that already declare "
                        "explicit Hs, rather than every atom (default false)."
                    ),
                },
            },
        },
    },
    {
        "name": "remove_hydrogens",
        "description": (
            "Strip explicit hydrogens from the current molecule (RDKit RemoveHs), "
            "leaving them implicit. An undo checkpoint is pushed."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "optimize_geometry",
        "description": (
            "Minimize the current molecule's existing 3D conformer with a force "
            "field (MMFF94 or UFF). Unlike trigger_3d_conversion, this refines "
            "coordinates the molecule already has rather than generating new "
            "ones — call trigger_3d_conversion first if there is no conformer yet. "
            "An undo checkpoint is pushed and the 3D view is refreshed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "force_field": {
                    "type": "string",
                    "enum": ["mmff", "uff"],
                    "description": "Which force field to minimize with (default 'mmff').",
                },
                "max_iters": {
                    "type": "integer",
                    "description": "Maximum optimizer iterations (default 500).",
                },
            },
        },
    },
    {
        "name": "set_atom_charge",
        "description": (
            "Set the formal charge of one atom by its 0-based RDKit index. "
            "The molecule is re-sanitized after the change and the call fails "
            "if that produces an invalid structure. Call get_mapped_smiles first "
            "to find the right atom_index. An undo checkpoint is pushed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "atom_index": {
                    "type": "integer",
                    "description": "0-based RDKit atom index.",
                },
                "charge": {
                    "type": "integer",
                    "description": "New formal charge, e.g. -1, 0, 1.",
                },
            },
            "required": ["atom_index", "charge"],
        },
    },
    {
        "name": "delete_atoms",
        "description": (
            "Delete one or more atoms from the current molecule by their 0-based "
            "RDKit indices. The molecule is re-sanitized after the deletion, and "
            "the call fails if that produces an invalid structure. Call "
            "get_mapped_smiles first to find the right indices. An undo "
            "checkpoint is pushed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "atom_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "0-based RDKit atom indices to remove.",
                },
            },
            "required": ["atom_indices"],
        },
    },
    {
        "name": "substructure_search",
        "description": (
            "Find every match of a SMARTS substructure pattern in the current "
            "molecule (RDKit GetSubstructMatches). Returns, for each match, the "
            "list of atom indices in pattern order. Read-only — nothing is changed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "smarts": {
                    "type": "string",
                    "description": "SMARTS pattern to search for, e.g. '[OH]' or 'c1ccccc1'.",
                },
                "unique_matches": {
                    "type": "boolean",
                    "description": (
                        "If true (default), matches that are symmetry-equivalent "
                        "are collapsed to one; if false, every match RDKit finds "
                        "is returned."
                    ),
                },
            },
            "required": ["smarts"],
        },
    },
    {
        "name": "compute_partial_charges",
        "description": (
            "Compute Gasteiger partial charges for the current molecule. "
            "Read-only — the result is computed on a private copy, so the "
            "molecule on the canvas is never modified."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "atom_indices": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Optional list of 0-based atom indices to restrict the "
                        "result to. Omit or pass [] for every atom."
                    ),
                },
            },
        },
    },
    # ------------------------------------------------------------------
    # Visual / 3D
    # ------------------------------------------------------------------
    {
        "name": "set_cpk_color_override",
        "description": (
            "Override the CPK display color of specific atoms in the 3D viewer. "
            "Useful for visually emphasizing active sites, selected atoms, or "
            "computed results. Overrides PERSIST across redraws until cleared "
            "with reset_cpk_color_override. (Formerly named highlight_atoms.)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "atom_colors": {
                    "type": "object",
                    "description": (
                        "Mapping of atom index (as string key) to hex color string "
                        '(e.g. {"0": "#FF0000", "3": "#00FF00"}).'
                    ),
                    "additionalProperties": {"type": "string"},
                }
            },
            "required": ["atom_colors"],
        },
    },
    {
        "name": "reset_cpk_color_override",
        "description": (
            "Clear color overrides set via set_cpk_color_override / "
            "set_bond_color_override and restore default element colors. "
            "scope: 'atoms', 'bonds', or 'all' (default 'all'). "
            "Redraws the 3D scene once."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["atoms", "bonds", "all"],
                    "description": "Which overrides to clear (default 'all').",
                },
            },
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
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": (
                        "Python source code to execute (normal multi-line "
                        "string). An array of lines is also accepted if your "
                        "client escapes newlines."
                    ),
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
        "name": "exit_3d_mode",
        "description": (
            "Switch the MoleditPy UI back to 2D editing mode. "
            "Restores the 2D drawing canvas and re-enables the editing tools "
            "(counterpart of enter_3d_mode)."
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
            "Use after color overrides (set_cpk_color_override / "
            "set_bond_color_override) "
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
        "name": "set_bond_color_override",
        "description": (
            "Override the display color of specific bonds in the 3D viewer. "
            "bond_colors maps bond index (as string key) to a hex color "
            '(e.g. {"0": "#FF0000", "3": "#0000FF"}). '
            "Overrides PERSIST across redraws until cleared with "
            "reset_cpk_color_override (scope 'bonds' or 'all'). "
            "(Formerly named highlight_bonds.)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bond_colors": {
                    "type": "object",
                    "description": 'Bond index → hex color, e.g. {"0": "#FF0000"}.',
                    "additionalProperties": {"type": "string"},
                },
                "atom_pair_colors": {
                    "type": "object",
                    "description": (
                        "'atomIndex1-atomIndex2' → hex color, e.g. "
                        '{"0-3": "#FF0000"} — colors the bond between the '
                        "two atoms. Easier than bond indices: use the atom "
                        "indices from get_mapped_smiles/get_atom_properties. "
                        "Errors if no bond exists between the pair."
                    ),
                    "additionalProperties": {"type": "string"},
                },
            },
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
            "Use this to inspect the real API before writing a plugin. "
            "Pass start_line/end_line to read only part of a large file — "
            "the natural follow-up to a grep_files hit."
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
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to return, 1-based (default 1).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to return, inclusive. Omit for end of file.",
                },
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
    {
        "name": "list_available_plugins",
        "description": (
            "Fetch the official MoleditPy plugin registry and list the plugins "
            "available for installation (name, version, tags, description). "
            "Optionally filter with a search term matched against name, "
            "description, and tags. Use this to discover functionality the "
            "user is missing (e.g. a specific input generator or analyzer), "
            "then suggest installing it via open_plugin_installer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Optional case-insensitive filter term.",
                },
            },
        },
    },
    {
        "name": "open_plugin_installer",
        "description": (
            "Open the Plugin Installer window inside MoleditPy so the user "
            "can install or update plugins from the official registry. "
            "Use after suggesting a plugin found via list_available_plugins. "
            "Errors with manual-install instructions if the Plugin Installer "
            "plugin itself is not installed."
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
            "Only extensions on the allowed list are accepted. "
            "NOTE: for files that must contain the current molecule's 3D "
            "coordinates (quantum-chemistry input files, .xyz exports), "
            "prefer write_file_with_xyz_block instead of pasting coordinates "
            "into 'content'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path, e.g. 'run1/molecule.inp'",
                },
                "content": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": (
                        "Text content to write (UTF-8) as a normal "
                        "multi-line string. An array of lines (joined with "
                        "newlines) is also accepted if your client escapes "
                        "newlines."
                    ),
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
        "name": "write_file_with_xyz_block",
        "description": (
            "PREFERRED tool for generating quantum-chemistry input files "
            "(Gaussian, ORCA, GAMESS, xTB, etc.). "
            "Writes a text file whose coordinate block is taken directly from the "
            "molecule currently loaded in MoleditPy — never retype coordinates into "
            "write_text_file, use this instead to avoid transcription errors. "
            "The file is composed as: header + XYZ coordinate block + footer. "
            "Put keywords, charge/multiplicity lines, etc. in 'header' and any "
            "trailing sections in 'footer'. The block itself is customisable: "
            "element column style, atom order/subset, and coordinate precision. "
            "Same sandbox rules as write_text_file (relative path, allowed "
            "extensions, overwrite flag)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path, e.g. 'run1/molecule.inp'",
                },
                "header": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": (
                        "Text placed before the coordinate block "
                        "(e.g. route section, charge and multiplicity). "
                        "Use a normal multi-line string (real newlines). "
                        "An array of lines is also accepted if your client "
                        "escapes newlines. A trailing newline is added if "
                        "missing."
                    ),
                },
                "footer": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": (
                        "Text placed after the coordinate block. "
                        "Multi-line string, or an array of lines, as with "
                        "header."
                    ),
                },
                "element_style": {
                    "type": "string",
                    "enum": ["symbol", "atomic_number", "symbol_and_number"],
                    "description": (
                        "Element column format: 'symbol' -> 'C' (default), "
                        "'atomic_number' -> '6', "
                        "'symbol_and_number' -> 'C 6.0' (GAMESS $DATA style)."
                    ),
                },
                "atom_order": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Optional list of 0-based RDKit atom indices defining the "
                        "output order (may also be a subset). Omit to keep the "
                        "molecule's native order. No duplicates allowed."
                    ),
                },
                "precision": {
                    "type": "integer",
                    "description": "Coordinate decimal places, 1-12 (default 6).",
                },
                "xyz_header": {
                    "type": "boolean",
                    "description": (
                        "Prepend the standard 2-line XYZ header (atom count + "
                        "comment) for .xyz files (default false = bare block)."
                    ),
                },
                "comment": {
                    "type": "string",
                    "description": "Comment line used when xyz_header=true.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Allow overwriting an existing file (default false).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_text_file",
        "description": (
            "Read and return the UTF-8 text content of a file inside the "
            "configured base directory (relative path) or inside a read-only "
            "folder the user approved (absolute path). "
            "Pass start_line/end_line to read only a slice of a large file "
            "(line numbers are 1-based and match grep_files output)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _READ_PATH_NOTE},
                "start_line": {
                    "type": "integer",
                    "description": "First line to return, 1-based (default 1).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to return, inclusive. Omit for end of file.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": (
            "List files and subdirectories at a path inside the base directory "
            "or a user-approved read-only folder (absolute path). "
            "Omit path (or use '.') to list the base directory itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Default '.' = base directory. " + _READ_PATH_NOTE,
                },
            },
        },
    },
    # ------------------------------------------------------------------
    # Code / text search
    # ------------------------------------------------------------------
    {
        "name": "grep_files",
        "description": (
            "Search file contents with a regular expression and return matching "
            "lines as 'relative/path.py:LINE: text' — the fastest way to find "
            "where something is defined or used. "
            "Choose the tree with 'root':\n"
            "  'app_source'  — the installed MoleditPy package source. Use this "
            "to find the real PluginContext API, signal names, or an example of "
            "how the app does something before writing a plugin.\n"
            "  'plugins'     — the user's plugin directory (~/.moleditpy/plugins).\n"
            "  'files'       — the configured file I/O base directory "
            "(calculation outputs, inputs, notes); only files with allowed "
            "extensions are searched.\n"
            "Follow a hit with get_app_source or read_text_file using "
            "start_line/end_line to read the surrounding code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "Python regular expression, e.g. 'def add_menu_action' or "
                        "'class \\\\w+Dialog'. Set fixed_string=true to search for "
                        "it literally instead."
                    ),
                },
                "root": {
                    "type": "string",
                    "enum": ["app_source", "plugins", "files"],
                    "description": "Which tree to search (default 'app_source').",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Optional sub-path within the root to narrow the search "
                        "(e.g. 'plugins' or 'core')."
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Filename filter, e.g. '*.py' (default) or '*.md'. "
                        "Use '*' to search every text file."
                    ),
                },
                "ignore_case": {
                    "type": "boolean",
                    "description": "Case-insensitive match (default false).",
                },
                "fixed_string": {
                    "type": "boolean",
                    "description": "Treat 'pattern' as a literal string (default false).",
                },
                "context": {
                    "type": "integer",
                    "description": (
                        "Lines of surrounding context to include per match, 0-10 "
                        "(default 0)."
                    ),
                },
                "max_matches": {
                    "type": "integer",
                    "description": "Stop after this many matches, 1-500 (default 100).",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "find_files",
        "description": (
            "List files whose name matches a glob pattern, recursively, inside "
            "one of the searchable trees. Use it to locate a module before "
            "reading it (e.g. pattern '*dialog*.py' in root 'app_source'). "
            "Same 'root' choices as grep_files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Filename glob, e.g. '*.py', 'plugin_*.py' (default '*').",
                },
                "root": {
                    "type": "string",
                    "enum": ["app_source", "plugins", "files"],
                    "description": "Which tree to search (default 'app_source').",
                },
                "path": {
                    "type": "string",
                    "description": "Optional sub-path within the root to search under.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum paths to return, 1-1000 (default 200).",
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
        "name": "request_read_folder",
        "description": (
            "Ask the user for READ-ONLY access to a folder outside the base "
            "directory. MoleditPy shows a Yes/No dialog to the person at the "
            "window; only a Yes adds it (the answer can take a while). After "
            "that, read_text_file, list_directory, load_xyz_file and "
            "compare_structures accept absolute paths inside it; writing and "
            "deleting stay confined to the base directory. Say why in "
            "'reason'. If the user declines, do not ask again for the same "
            "folder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path of an existing folder.",
                },
                "reason": {
                    "type": "string",
                    "description": "Shown to the user in the dialog.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_file_io_config",
        "description": (
            "Get the current file I/O sandbox configuration: base directory, "
            "allowed file extensions and user-approved read-only folders."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_file_io_config",
        "description": (
            "Configure the file I/O sandbox. The user must approve every "
            "change in a MoleditPy dialog (the answer can take a while); a "
            "declined request changes nothing. "
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
# Tool annotations (behaviour hints — clients use them to decide what needs
# confirmation, what can be retried, and what merely reads state)
# ---------------------------------------------------------------------------

_READ_ONLY_TOOLS = {
    "get_current_molecule",
    "get_molecule_xyz",
    "get_atom_properties",
    "get_bond_info",
    "get_selected_atoms",
    "get_mapped_smiles",
    "get_app_info",
    "get_plugin_dir",
    "get_plugin_dev_manual",
    "list_app_source_tree",
    "get_app_source",
    "list_available_plugins",
    "check_chemistry",
    "read_text_file",
    "list_directory",
    "get_file_io_config",
    "grep_files",
    "find_files",
    "get_molecule_image",
    "get_molecule_descriptors",
    "substructure_search",
    "compute_partial_charges",
    "get_3d_camera",
    "measure_geometry",
}

#: Tools that replace or erase user work (the canvas, or a file on disk).
_DESTRUCTIVE_TOOLS = {
    "load_molecule_from_smiles",
    "load_from_mol_block",
    "load_molecule_by_name",
    "show_xyz_in_viewer",
    "apply_reaction_smarts",
    "clear_canvas",
    "write_text_file",
    "write_file_with_xyz_block",
    "delete_file",
    "run_python",
    "add_hydrogens",
    "remove_hydrogens",
    "optimize_geometry",
    "set_atom_charge",
    "delete_atoms",
    "load_xyz_file",
    "save_molecule_image",
    "edit_bonds",
}

#: Mutating tools whose repeated call leaves the same state.
_IDEMPOTENT_TOOLS = {
    "set_cpk_color_override",
    "reset_cpk_color_override",
    "set_bond_color_override",
    "highlight_bonds",
    "enter_3d_mode",
    "exit_3d_mode",
    "fit_2d_view",
    "reset_3d_camera",
    "refresh_3d_view",
    "refresh_ui",
    "reload_plugins",
    "open_plugin_installer",
    "set_file_io_config",
    "trigger_3d_conversion",
    "set_3d_camera",
    "clear_overlay",
    "set_3d_style",
    "request_read_folder",
}

#: Tools that reach outside MoleditPy (network).
_OPEN_WORLD_TOOLS = {
    "load_molecule_by_name",
    "list_available_plugins",
    "get_plugin_dev_manual",
}


def _apply_annotations() -> None:
    """Attach MCP behaviour hints to every tool definition."""
    for tool in _TOOLS:
        name = tool["name"]
        read_only = name in _READ_ONLY_TOOLS
        annotations: dict[str, Any] = {
            "readOnlyHint": read_only,
            "openWorldHint": name in _OPEN_WORLD_TOOLS,
        }
        if not read_only:
            annotations["destructiveHint"] = name in _DESTRUCTIVE_TOOLS
            annotations["idempotentHint"] = name in _IDEMPOTENT_TOOLS
        tool["annotations"] = annotations


_apply_annotations()


# ---------------------------------------------------------------------------
# Tool result helpers
# ---------------------------------------------------------------------------


def _tool_ok(text: str) -> dict[str, Any]:
    """Return a successful MCP tool result."""
    return {"content": [{"type": "text", "text": text}]}


def _tool_err(text: str) -> dict[str, Any]:
    """Return a failed MCP tool result."""
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _tool_image(data_base64: str, mime_type: str, caption: str = "") -> dict[str, Any]:
    """Return a successful MCP tool result carrying an image content block.

    A leading text block is included when *caption* is given: several MCP
    clients render only the first content block's type as the "kind" of
    result, and a caption-only response with no image block at all is a
    worse failure than a caption a strict client ignores.
    """
    content: list[dict[str, Any]] = []
    if caption:
        content.append({"type": "text", "text": caption})
    content.append({"type": "image", "data": data_base64, "mimeType": mime_type})
    return {"content": content}


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
        ) from None
    return resolved


def _check_extension(path: Path, allowed_extensions: list[str]) -> None:
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


def normalize_extensions(raw: Any) -> list[str]:
    """Validate an extension allowlist and bring every entry to '.ext' form.

    A bare string is rejected rather than iterated: ``".inp"`` would otherwise
    become the allowlist ``['..', '.i', '.n', '.p']``.
    """
    if not isinstance(raw, (list, tuple)) or not all(isinstance(e, str) for e in raw):
        raise ValueError(
            "'allowed_extensions' must be a list of strings, e.g. ['.inp', '.xyz']"
        )
    exts: list[str] = []
    for entry in raw:
        ext = entry.strip().lower()
        if not ext or ext == ".":
            raise ValueError("'allowed_extensions' contains an empty extension")
        ext = ext if ext.startswith(".") else f".{ext}"
        if ext not in exts:
            exts.append(ext)
    return exts


def _get_sandbox(bridge: Any) -> tuple[str, list[str]]:
    """
    Fetch the current file I/O config from the bridge.

    Returns (base_dir, allowed_extensions).
    Raises ValueError if base_dir is not configured.
    """
    cfg = bridge.call("get_file_io_config")
    base_dir: str | None = cfg.get("base_dir")
    if not base_dir:
        raise ValueError(
            "File I/O base directory is not configured. "
            "Call set_file_io_config with a base_dir first."
        )
    allowed: list[str] = cfg.get("allowed_extensions", [])
    return base_dir, allowed


#: How long the server waits for the user to answer an approval dialog. The
#: dialog itself gives up earlier (bridge.APPROVAL_TIMEOUT_MS), so an answer
#: can never arrive after the client has been told the call timed out.
APPROVAL_WAIT_SECONDS = 300.0


def _image_call_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments of the bridge's get_molecule_image, shared by the two image tools."""
    call_args: dict[str, Any] = {
        "view": arguments.get("view", "auto"),
        "width": arguments.get("width"),
        "height": arguments.get("height"),
    }
    if arguments.get("atom_labels"):
        call_args["atom_labels"] = True
    if arguments.get("background") is not None:
        call_args["background"] = arguments["background"]
    return call_args


def _resolve_read_path(bridge: Any, user_path: str) -> tuple[Path, list[str]]:
    """Resolve a path for READING: relative to the base directory as before,
    or absolute inside the base or a user-approved read-only folder.

    Returns (resolved path, allowed extensions). Writing never goes through
    here, so the read-only folders cannot be written to.
    """
    cfg = bridge.call("get_file_io_config")
    base_dir: str | None = cfg.get("base_dir")
    allowed: list[str] = cfg.get("allowed_extensions", [])
    roots = [r for r in cfg.get("read_roots", []) if isinstance(r, str)]
    if not Path(user_path).is_absolute():
        if not base_dir:
            raise ValueError(
                "File I/O base directory is not configured. "
                "Call set_file_io_config with a base_dir first."
            )
        return _resolve_safe_path(user_path, base_dir), allowed
    target = Path(user_path).expanduser().resolve()
    for root in ([base_dir] if base_dir else []) + roots:
        base = Path(root).expanduser().resolve()
        try:
            target.relative_to(base)
        except ValueError:
            continue
        return target, allowed
    raise ValueError(
        f"{user_path!r} is outside the base directory and the read-only "
        "folders. Ask the user with request_read_folder first."
    )


def _read_sandbox_text(bridge: Any, user_path: str) -> str:
    """UTF-8 text of a sandbox file, with the same checks as read_text_file."""
    target, allowed_exts = _resolve_read_path(bridge, user_path)
    _check_extension(target, allowed_exts)
    if not target.is_file():
        raise ValueError(f"{user_path!r} does not exist or is not a file.")
    size = target.stat().st_size
    if size > _MAX_FILE_BYTES:
        raise ValueError(
            f"File is {size:,} bytes, exceeding the "
            f"{_MAX_FILE_BYTES // 1024 // 1024} MB read limit."
        )
    return target.read_text(encoding="utf-8")


def _show_xyz(
    bridge: Any, xyz_text: str, source_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Shared body of show_xyz_in_viewer and load_xyz_file."""
    call_args: dict[str, Any] = {"xyz_text": xyz_text, "source_name": source_name}
    for key in ("charge", "skip_chemistry", "frame", "keep_camera"):
        if arguments.get(key) is not None:
            call_args[key] = arguments[key]
    result = bridge.call("show_xyz", call_args)
    if not result.get("success"):
        return _tool_err("Failed to parse XYZ data. Verify the format.")
    text = f"XYZ data displayed in 3D viewer (source: {source_name})."
    if "num_frames" in result:
        text += f" Frame {result['frame']} of {result['num_frames']} (0-based)."
    if "num_atoms" in result:
        if result.get("chemistry_skipped"):
            text += f" {result['num_atoms']} atoms, {result['num_bonds']} bonds by distance (chemistry skipped)."
        else:
            text += (
                f" {result['num_atoms']} atoms, {result['num_bonds']} bonds, "
                f"charge {result.get('charge')}."
            )
    if result.get("note"):
        text += " " + result["note"]
    return _tool_ok(text)


# ---------------------------------------------------------------------------
# Code / text search helpers (run in the server thread — no Qt needed beyond
# resolving the root directory)
# ---------------------------------------------------------------------------

_SEARCH_SKIP_DIRS = {
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    ".idea",
    ".vscode",
}

#: Suffixes searched in the source/plugin trees (the sandbox root uses the
#: user's own extension allowlist instead).
_SEARCH_TEXT_SUFFIXES = {
    ".py",
    ".pyw",
    ".pyi",
    ".md",
    ".txt",
    ".json",
    ".toml",
    ".cfg",
    ".ini",
    ".yaml",
    ".yml",
    ".rst",
    ".csv",
    ".xyz",
    ".inp",
    ".out",
    ".log",
    ".sh",
    ".bat",
    ".html",
    ".css",
    ".js",
    ".ts",
}

_GREP_MAX_FILE_BYTES = 2 * 1024 * 1024
_GREP_MAX_FILES = 20_000
_GREP_MAX_LINE_CHARS = 300


def _resolve_search_root(
    bridge: Any, root: str, sub_path: str
) -> tuple[Path, Path, list[str] | None]:
    """
    Resolve a search *root* name to directories.

    Returns (base, start, allowed_extensions) where *start* is *base* narrowed
    by *sub_path*, and allowed_extensions is None for the source/plugin trees.
    """
    if root == "files":
        base_dir, allowed = _get_sandbox(bridge)
        base = Path(base_dir).expanduser().resolve()
        exts: list[str] | None = [e.lower() for e in allowed]
    elif root == "app_source":
        base = Path(bridge.call("get_app_source_root")["root"]).resolve()
        exts = None
    elif root == "plugins":
        base = Path(bridge.call("get_plugin_dir")["plugin_dir"]).expanduser().resolve()
        exts = None
    else:
        raise ValueError(
            f"Unknown root {root!r}. Use 'app_source', 'plugins', or 'files'."
        )
    if not base.is_dir():
        raise ValueError(f"The {root!r} directory does not exist: {base}")

    start = base
    if sub_path:
        if Path(sub_path).is_absolute():
            raise ValueError("'path' must be relative to the selected root.")
        start = (base / sub_path).resolve()
        try:
            start.relative_to(base)
        except ValueError:
            raise ValueError(
                f"Path {sub_path!r} resolves outside the {root!r} root."
            ) from None
        if not start.is_dir():
            raise ValueError(
                f"{sub_path!r} is not a directory inside the {root!r} root."
            )
    return base, start, exts


def _walk_files(start: Path, name_glob: str) -> Any:
    """Yield files under *start* whose name matches *name_glob*, in sorted order.

    Cache/VCS/virtualenv directories are pruned *below* *start* only: testing
    the absolute path instead would skip everything when the tree itself
    lives under one (e.g. MoleditPy installed in ``.venv/``), and walking into
    them before filtering wastes the whole search on ``node_modules``.
    """
    pattern = name_glob or "*"
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if d not in _SEARCH_SKIP_DIRS)
        for filename in sorted(filenames):
            if fnmatch.fnmatch(filename, pattern):
                yield Path(dirpath, filename)


def _iter_search_files(
    start: Path, name_glob: str, allowed_exts: list[str] | None
) -> Any:
    """Yield candidate text files under *start*, capped at _GREP_MAX_FILES."""
    count = 0
    for path in _walk_files(start, name_glob):
        if count >= _GREP_MAX_FILES:
            return
        suffix = path.suffix.lower()
        if allowed_exts is not None:
            if suffix not in allowed_exts:
                continue
        elif suffix not in _SEARCH_TEXT_SUFFIXES:
            continue
        count += 1
        yield path


def run_grep(
    start: Path,
    base: Path,
    pattern: str,
    name_glob: str = "*.py",
    allowed_exts: list[str] | None = None,
    ignore_case: bool = False,
    fixed_string: bool = False,
    context: int = 0,
    max_matches: int = 100,
) -> str:
    """Search file contents under *start* and format the matches for an LLM."""
    if not pattern:
        raise ValueError("'pattern' argument is required.")
    context = max(0, min(int(context), 10))
    max_matches = max(1, min(int(max_matches), 500))
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(re.escape(pattern) if fixed_string else pattern, flags)
    except re.error as exc:
        raise ValueError(
            f"Invalid regular expression {pattern!r}: {exc}. "
            "Pass fixed_string=true to search for it literally."
        ) from exc

    out: list[str] = []
    matches = 0
    files_with_matches = 0
    truncated = False
    for path in _iter_search_files(start, name_glob, allowed_exts):
        if truncated:
            break
        try:
            if path.stat().st_size > _GREP_MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\x00" in text[:4096]:  # binary
            continue
        lines = text.splitlines()
        hits = [i for i, line in enumerate(lines) if regex.search(line)]
        if not hits:
            continue
        files_with_matches += 1
        rel = path.relative_to(base).as_posix()
        shown: set = set()
        for i in hits:
            if matches >= max_matches:
                truncated = True
                break
            matches += 1
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                if j in shown:
                    continue
                shown.add(j)
                sep = ":" if j == i else "-"
                body = lines[j].rstrip()
                if len(body) > _GREP_MAX_LINE_CHARS:
                    body = body[:_GREP_MAX_LINE_CHARS] + " …"
                out.append(f"{rel}{sep}{j + 1}{sep} {body}")

    if not out:
        return (
            f"No matches for {pattern!r} in {base} "
            f"(glob {name_glob or '*'}).\n"
            "Try a broader pattern, ignore_case=true, or glob='*'."
        )
    header = f"{matches} match(es) in {files_with_matches} file(s) under {base}" + (
        " — truncated, refine the pattern or raise max_matches" if truncated else ""
    )
    return header + ":\n" + "\n".join(out)


def run_find(
    start: Path, base: Path, name_glob: str = "*", max_results: int = 200
) -> str:
    """List file paths under *start* matching *name_glob*."""
    max_results = max(1, min(int(max_results), 1000))
    found: list[str] = []
    truncated = False
    for path in _walk_files(start, name_glob):
        if len(found) >= max_results:
            truncated = True
            break
        found.append(
            f"{path.relative_to(base).as_posix()}  ({path.stat().st_size:,} bytes)"
        )
    if not found:
        return f"No files matching {name_glob or '*'} under {start}."
    header = f"{len(found)} file(s) under {base}" + (
        " — truncated, raise max_results" if truncated else ""
    )
    return header + ":\n" + "\n".join(found)


def _slice_lines(text: str, start_line: Any, end_line: Any) -> str:
    """Return the 1-based [start_line, end_line] slice of *text*, annotated."""
    if start_line is None and end_line is None:
        return text
    lines = text.splitlines()
    first = max(1, int(start_line or 1))
    last = len(lines) if end_line is None else min(len(lines), int(end_line))
    if first > len(lines):
        raise ValueError(
            f"start_line {first} is past the end of the file ({len(lines)} lines)."
        )
    if last < first:
        raise ValueError("end_line must be greater than or equal to start_line.")
    body = "\n".join(lines[first - 1 : last])
    return f"[lines {first}-{last} of {len(lines)}]\n{body}"


def _str_arg(arguments: dict[str, Any], key: str, default: str = "") -> str:
    """A stripped string tool argument; *default* when absent, null, or blank.

    ``arguments.get(key, "").strip()`` crashes with AttributeError when a
    client sends ``null`` or a number, and a blank value would otherwise
    override *default*.
    """
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key!r} must be a string, not {type(value).__name__}.")
    return value.strip() or default


def _text_arg(value: Any) -> str:
    """
    Accept a tool text argument as either a string or a list of lines.

    A list is joined with newlines — the unambiguous way for MCP clients
    to pass multi-line content (some deliver literal backslash-n instead
    of real newlines inside plain strings).
    """
    if isinstance(value, list):
        return "\n".join(str(line) for line in value)
    return str(value) if value is not None else ""


def format_xyz_block(
    atoms: list[dict[str, Any]],
    element_style: str = "symbol",
    atom_order: list[int] | None = None,
    precision: int = 6,
) -> str:
    """
    Format per-atom records into an XYZ coordinate block (no trailing newline).

    *atoms* is the list returned by the bridge's ``get_xyz_atoms`` operation.
    Raises ValueError on an invalid element_style, precision, or atom_order.
    """
    if element_style not in ("symbol", "atomic_number", "symbol_and_number"):
        raise ValueError(
            f"Unknown element_style {element_style!r}. "
            "Use 'symbol', 'atomic_number', or 'symbol_and_number'."
        )
    if not 1 <= precision <= 12:
        raise ValueError("precision must be between 1 and 12.")

    by_index = {a["index"]: a for a in atoms}
    if atom_order is None:
        selected = atoms
    else:
        if len(set(atom_order)) != len(atom_order):
            raise ValueError("atom_order contains duplicate indices.")
        bad = [i for i in atom_order if i not in by_index]
        if bad:
            raise ValueError(
                f"atom_order contains invalid atom indices: {bad} "
                f"(valid range: 0..{len(atoms) - 1})."
            )
        selected = [by_index[i] for i in atom_order]

    width = precision + 8  # room for sign, 4 integer digits, and the point
    lines = []
    for a in selected:
        if element_style == "symbol":
            elem = f"{a['symbol']:<3}"
        elif element_style == "atomic_number":
            elem = f"{a['atomic_num']:<3d}"
        else:  # symbol_and_number (GAMESS $DATA style)
            elem = f"{a['symbol']:<3} {float(a['atomic_num']):>5.1f}"
        lines.append(
            f"{elem} "
            f"{a['x']:>{width}.{precision}f} "
            f"{a['y']:>{width}.{precision}f} "
            f"{a['z']:>{width}.{precision}f}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PubChem helper (runs in server thread — no Qt needed)
# ---------------------------------------------------------------------------


_PLUGIN_DEV_MANUAL_URL = "https://hiroyokoyama.github.io/python_molecular_editor/docs/PLUGIN_DEVELOPMENT_MANUAL_V4.md"

_PLUGIN_REGISTRY_URL = (
    "https://hiroyokoyama.github.io/moleditpy-plugins/REGISTRY/plugins.json"
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


def dispatch_tool(
    bridge: Any,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """
    Dispatch a named MCP tool call through *bridge* and return the result dict.

    All calls to *bridge.call()* block until the Qt main thread processes them.
    """
    try:
        if name == "get_current_molecule":
            info = bridge.call("get_molecule_info")
            if not info["loaded"]:
                return _tool_ok("No molecule is currently loaded in MoleditPy.")
            text = (
                f"SMILES: {info['smiles'] if info['smiles'] is not None else '(not available)'}\n"
                f"Formula: {info['formula']}\n"
                f"Molecular Weight: {info['molecular_weight']:.4f} g/mol\n"
                f"Atoms: {info['num_atoms']}\n"
                f"Bonds: {info['num_bonds']}\n"
                f"3D coordinates: "
                f"{'available' if info['has_3d_coords'] else 'not available'}"
            )
            if info.get("note"):
                text += f"\nNote: {info['note']}"
            return _tool_ok(text)

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
                return _tool_ok("No molecule loaded or molecule has no bonds.")
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
            smiles = _str_arg(arguments, "smiles")
            if not smiles:
                return _tool_err("'smiles' argument is required.")
            bridge.call("load_smiles", {"smiles": smiles})
            return _tool_ok(f"Molecule loaded from SMILES: {smiles}")

        if name == "load_from_mol_block":
            mol_block = _text_arg(arguments.get("mol_block", "")).strip()
            if not mol_block:
                return _tool_err("'mol_block' argument is required.")
            result = bridge.call("load_mol_block", {"mol_block": mol_block})
            if result["success"]:
                return _tool_ok("Molecule loaded from MOL block.")
            return _tool_err("Failed to parse MOL block. Check the format.")

        if name == "show_xyz_in_viewer":
            xyz_text = _text_arg(arguments.get("xyz_text", "")).strip()
            source_name = arguments.get("source_name", "MCP input")
            if not xyz_text:
                return _tool_err("'xyz_text' argument is required.")
            return _show_xyz(bridge, xyz_text, source_name, arguments)

        if name == "load_xyz_file":
            user_path = _str_arg(arguments, "path")
            if not user_path:
                return _tool_err("'path' argument is required.")
            xyz_text = _read_sandbox_text(bridge, user_path)
            return _show_xyz(bridge, xyz_text, user_path, arguments)

        if name == "trigger_3d_conversion":
            # The RDKit fallback (ETKDG embed + MMFF optimize) runs in-thread
            # and can exceed the default 10 s on larger molecules.
            bridge.call("trigger_3d_conversion", timeout=60.0)
            return _tool_ok(
                "3D conversion triggered. "
                "Use get_molecule_xyz to retrieve the generated coordinates."
            )

        if name == "get_molecule_image":
            data = bridge.call("get_molecule_image", _image_call_args(arguments))
            return _tool_image(
                data["image_base64"],
                data["mime_type"],
                caption=f"{data['view'].upper()} view, {data['width']}x{data['height']}",
            )

        if name == "save_molecule_image":
            user_path = _str_arg(arguments, "path")
            if not user_path:
                return _tool_err("'path' argument is required.")
            base_dir, _ = _get_sandbox(bridge)
            target = _resolve_safe_path(user_path, base_dir)
            if target.suffix.lower() != ".png":
                return _tool_err(
                    "save_molecule_image writes PNG only: the path must end in '.png'."
                )
            if target.exists() and not bool(arguments.get("overwrite", False)):
                return _tool_err(
                    f"{user_path!r} already exists. Pass overwrite=true to replace it."
                )
            data = bridge.call("get_molecule_image", _image_call_args(arguments))
            png = base64.b64decode(data["image_base64"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png)
            return _tool_ok(
                f"Saved {data['view'].upper()} view ({data['width']}x{data['height']}) "
                f"to {user_path} ({len(png):,} bytes)"
            )

        if name == "get_3d_camera":
            return _tool_ok(json.dumps(bridge.call("get_3d_camera")))

        if name == "set_3d_camera":
            keys = (
                "position",
                "direction",
                "focal_point",
                "view_up",
                "fit",
                "zoom",
                "direction_atoms",
                "plane_atoms",
                "focal_atoms",
                "parallel_projection",
            )
            cam_args = {k: arguments[k] for k in keys if arguments.get(k) is not None}
            return _tool_ok(
                "Camera set: " + json.dumps(bridge.call("set_3d_camera", cam_args))
            )

        if name == "measure_geometry":
            data = bridge.call("measure_geometry", {"atoms": arguments.get("atoms")})
            lines = []
            for m in data["measurements"]:
                label = "-".join(f"{s}{i}" for s, i in zip(m["symbols"], m["atoms"]))
                unit = " A" if m["type"] == "distance" else " deg"
                lines.append(f"{m['type']:<8} {label}: {m['value']:.4f}{unit}")
            return _tool_ok("\n".join(lines))

        if name == "compare_structures":
            xyz_text = _text_arg(arguments.get("xyz_text", "")).strip()
            user_path = _str_arg(arguments, "path")
            if bool(xyz_text) == bool(user_path):
                return _tool_err("Pass exactly one of 'xyz_text' or 'path'.")
            if user_path:
                xyz_text = _read_sandbox_text(bridge, user_path)
            cmp_args: dict[str, Any] = {"xyz_text": xyz_text}
            for key in (
                "frame",
                "align",
                "heavy_atoms_only",
                "overlay",
                "overlay_color",
                "current_color",
            ):
                if arguments.get(key) is not None:
                    cmp_args[key] = arguments[key]
            data = bridge.call("compare_structures", cmp_args)
            lines = [
                f"RMSD: {data['rmsd']:.4f} A over {data['atoms_used']} atoms "
                f"({'aligned' if data['aligned'] else 'not aligned'})",
                "Largest deviations:",
            ]
            lines += [
                f"  {d['symbol']}{d['index']}: {d['deviation']:.4f} A"
                for d in data["largest_deviations"]
            ]
            if data.get("overlay"):
                lines.append(
                    "Overlay drawn: stick style, carbons colored per structure "
                    "(clear_overlay removes it and restores the view)."
                )
            return _tool_ok("\n".join(lines))

        if name == "set_3d_style":
            data = bridge.call("set_3d_style", {"style": arguments.get("style")})
            return _tool_ok(
                f"3D style: {data['style']} (was {data.get('previous') or 'unknown'})."
            )

        if name == "edit_bonds":
            bond_args = {
                k: arguments[k]
                for k in ("add", "remove", "bond_type")
                if arguments.get(k) is not None
            }
            data = bridge.call("edit_bonds", bond_args)
            if not data.get("changed"):
                return _tool_ok(
                    "No change. Skipped: "
                    + "; ".join(
                        f"{sk['atoms'][0]}-{sk['atoms'][1]} ({sk['reason']})"
                        for sk in data["skipped"]
                    )
                )
            parts = []
            if data["added"]:
                parts.append(
                    "Added: " + ", ".join(f"{i}-{j}" for i, j in data["added"])
                )
            if data["removed"]:
                parts.append(
                    "Removed: " + ", ".join(f"{i}-{j}" for i, j in data["removed"])
                )
            if data["skipped"]:
                parts.append(
                    "Skipped: "
                    + "; ".join(
                        f"{sk['atoms'][0]}-{sk['atoms'][1]} ({sk['reason']})"
                        for sk in data["skipped"]
                    )
                )
            parts.append(f"{data['num_bonds']} bonds now (undo step recorded).")
            if not data.get("sanitized"):
                parts.append(
                    "Not sanitized: the result breaks normal valence rules, "
                    "so SMILES-based tools may not work on it."
                )
            return _tool_ok("\n".join(parts))

        if name == "request_read_folder":
            folder = _str_arg(arguments, "path")
            if not folder:
                return _tool_err("'path' argument is required.")
            # The user answers a dialog: allow minutes, not the default 10 s.
            data = bridge.call(
                "request_read_folder",
                {"path": folder, "reason": _str_arg(arguments, "reason", "")},
                timeout=APPROVAL_WAIT_SECONDS,
            )
            roots = ", ".join(data.get("read_roots", [])) or "(none)"
            if data.get("already_readable"):
                return _tool_ok(
                    f"{folder} is already readable (inside the base directory "
                    f"or an approved folder). Read-only folders: {roots}"
                )
            if data.get("declined"):
                return _tool_err(
                    f"The user declined read access to {folder}. Do not ask again "
                    "for this folder; work inside the base directory instead."
                )
            return _tool_ok(f"Read-only access granted. Read-only folders: {roots}")

        if name == "clear_overlay":
            data = bridge.call("clear_overlay")
            text = f"Overlay cleared ({data['removed']} actors removed)."
            if data.get("restored_style"):
                text += f" 3D style restored to {data['restored_style']}."
            return _tool_ok(text)

        # ------------------------------------------------------------------
        # Molecule manipulation (direct RDKit access)
        # ------------------------------------------------------------------

        if name == "get_molecule_descriptors":
            data = bridge.call("get_molecule_descriptors")
            if not data["loaded"]:
                return _tool_ok("No molecule is currently loaded in MoleditPy.")
            lines = [
                f"Canonical SMILES: {data['canonical_smiles']}",
                f"Formula: {data['formula']}",
                f"Molecular Weight: {data['molecular_weight']:.4f} g/mol",
                f"Exact Mass: {data['exact_mass']:.4f}",
                f"LogP (Crippen): {data['logp']:.4f}",
                f"TPSA: {data['tpsa']:.4f}",
                f"Formal Charge: {data['formal_charge']}",
                f"H-Bond Donors: {data['num_h_donors']}",
                f"H-Bond Acceptors: {data['num_h_acceptors']}",
                f"Rotatable Bonds: {data['num_rotatable_bonds']}",
                f"Rings: {data['num_rings']} ({data['num_aromatic_rings']} aromatic)",
                f"Atoms: {data['num_atoms']} ({data['num_heavy_atoms']} heavy)",
                f"Bonds: {data['num_bonds']}",
            ]
            return _tool_ok("\n".join(lines))

        if name == "add_hydrogens":
            result = bridge.call(
                "add_hydrogens",
                {"explicit_only": bool(arguments.get("explicit_only", False))},
            )
            return _tool_ok(
                f"Hydrogens added. Molecule now has {result['num_atoms']} atom(s)."
            )

        if name == "remove_hydrogens":
            result = bridge.call("remove_hydrogens")
            return _tool_ok(
                f"Hydrogens removed. Molecule now has {result['num_atoms']} atom(s)."
            )

        if name == "optimize_geometry":
            result = bridge.call(
                "optimize_geometry",
                {
                    "force_field": arguments.get("force_field", "mmff"),
                    "max_iters": arguments.get("max_iters", 500),
                },
                timeout=60.0,
            )
            status = "converged" if result["converged"] else "did not fully converge"
            return _tool_ok(
                f"Geometry optimized with {result['force_field'].upper()} ({status})."
            )

        if name == "set_atom_charge":
            if "atom_index" not in arguments:
                return _tool_err("'atom_index' argument is required.")
            if "charge" not in arguments:
                return _tool_err("'charge' argument is required.")
            result = bridge.call(
                "set_atom_charge",
                {"atom_index": arguments["atom_index"], "charge": arguments["charge"]},
            )
            return _tool_ok(
                f"Atom {result['atom_index']} formal charge set to {result['charge']}."
            )

        if name == "delete_atoms":
            atom_indices = arguments.get("atom_indices") or []
            if not atom_indices:
                return _tool_err("'atom_indices' argument is required.")
            result = bridge.call("delete_atoms", {"atom_indices": atom_indices})
            return _tool_ok(
                f"Deleted atom(s) {result['deleted']}. "
                f"{result['remaining_atoms']} atom(s) remain."
            )

        if name == "substructure_search":
            smarts = _str_arg(arguments, "smarts")
            if not smarts:
                return _tool_err("'smarts' argument is required.")
            data = bridge.call(
                "substructure_search",
                {
                    "smarts": smarts,
                    "unique_matches": bool(arguments.get("unique_matches", True)),
                },
            )
            if not data["loaded"]:
                return _tool_ok("No molecule is currently loaded in MoleditPy.")
            if not data["matches"]:
                return _tool_ok(f"No matches for SMARTS {smarts!r}.")
            lines = [f"{data['num_matches']} match(es) for SMARTS {smarts!r}:"]
            for i, match in enumerate(data["matches"]):
                lines.append(f"  Match {i}: atom indices {match}")
            return _tool_ok("\n".join(lines))

        if name == "compute_partial_charges":
            data = bridge.call(
                "compute_partial_charges",
                {"atom_indices": arguments.get("atom_indices") or []},
            )
            if not data["charges"]:
                return _tool_ok("No molecule loaded or no matching atoms found.")
            lines = ["Gasteiger partial charges:"]
            for entry in data["charges"]:
                charge = entry["charge"]
                shown = (
                    "n/a (no Gasteiger parameters)"
                    if charge is None
                    else f"{charge:+.4f}"
                )
                lines.append(f"  Atom {entry['index']} ({entry['symbol']}): {shown}")
            return _tool_ok("\n".join(lines))

        # "highlight_atoms" kept as a hidden alias for pre-1.4.0 clients.
        if name in ("set_cpk_color_override", "highlight_atoms"):
            atom_colors = arguments.get("atom_colors")
            if not atom_colors:
                return _tool_err("'atom_colors' argument is required.")
            bridge.call("highlight_atoms", {"atom_colors": atom_colors})
            return _tool_ok(
                f"CPK color override set for {len(atom_colors)} atom(s); "
                "persists across redraws until reset_cpk_color_override."
            )

        if name == "reset_cpk_color_override":
            data = bridge.call(
                "reset_cpk_color_override",
                {"scope": arguments.get("scope", "all")},
            )
            return _tool_ok(
                f"CPK color overrides cleared: {data['cleared_atoms']} atom(s), "
                f"{data['cleared_bonds']} bond(s)."
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
            code = _text_arg(arguments.get("code", "")).strip()
            if not code:
                return _tool_err("'code' argument is required.")
            result = bridge.call("run_python", {"code": code}, timeout=30.0)
            parts: list[str] = []
            if result.get("stdout"):
                parts.append(f"stdout:\n{result['stdout']}")
            if result.get("stderr"):
                parts.append(f"stderr:\n{result['stderr']}")
            res_repr = result.get("result", "None")
            if res_repr != "None":
                parts.append(f"result: {res_repr}")
            return _tool_ok("\n".join(parts) or "(no output)")

        if name == "load_molecule_by_name":
            mol_name = _str_arg(arguments, "name")
            if not mol_name:
                return _tool_err("'name' argument is required.")
            smiles = _fetch_smiles_by_name(mol_name)
            bridge.call("load_smiles", {"smiles": smiles})
            return _tool_ok(f"Loaded {mol_name!r} from PubChem.\nSMILES: {smiles}")

        if name == "push_undo_checkpoint":
            bridge.call("push_undo_checkpoint")
            return _tool_ok("Undo checkpoint pushed.")

        if name == "enter_3d_mode":
            bridge.call("enter_3d_mode")
            return _tool_ok("Switched to 3D viewer mode.")

        if name == "get_mapped_smiles":
            data = bridge.call("get_mapped_smiles")
            if not data["loaded"]:
                return _tool_ok("No molecule is currently loaded in MoleditPy.")
            legend = "\n".join(
                f"  atom_index {a['index']}: {a['symbol']} (shown as :{a['map_num']})"
                for a in data["atoms"]
            )
            return _tool_ok(
                f"Mapped SMILES (atom map number = atom_index + 1):\n"
                f"{data['mapped_smiles']}\n\n"
                f"Atom legend:\n{legend}\n\n"
                "Use the 0-based atom_index values with apply_reaction_smarts, "
                "set_cpk_color_override, and get_atom_properties."
            )

        if name == "apply_reaction_smarts":
            data = bridge.call(
                "apply_reaction_smarts",
                {
                    "reaction_smarts": arguments.get("reaction_smarts", ""),
                    "atom_index": arguments.get("atom_index"),
                    "convert_to_3d": arguments.get("convert_to_3d", True),
                },
            )
            conv_note = (
                "2D->3D conversion triggered."
                if data.get("converted_3d")
                else "3D conversion skipped."
            )
            mapped_note = ""
            if data.get("mapped_smiles"):
                mapped_note = (
                    f"\nWARNING: atom indices were reassigned — previous "
                    f"atom_index values are no longer valid.\n"
                    f"New mapped SMILES (map number = atom_index + 1):\n"
                    f"{data['mapped_smiles']}"
                )
            return _tool_ok(
                f"Transformation applied.\n"
                f"Rule: {arguments.get('reaction_smarts')}\n"
                f"New SMILES: {data['smiles']}\n"
                f"({data['num_products']} candidate product(s); "
                f"applied match #{data['selected_product']}) {conv_note}"
                f"{mapped_note}"
            )

        if name == "exit_3d_mode":
            bridge.call("exit_3d_mode")
            return _tool_ok("Switched back to 2D editing mode.")

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

        # "highlight_bonds" kept as a hidden alias for pre-1.4.1 clients.
        if name in ("set_bond_color_override", "highlight_bonds"):
            bond_colors = arguments.get("bond_colors")
            pair_colors = arguments.get("atom_pair_colors")
            if not bond_colors and not pair_colors:
                return _tool_err(
                    "Provide 'bond_colors' (bond index → color) and/or "
                    "'atom_pair_colors' ('i-j' atom pair → color)."
                )
            data = bridge.call(
                "highlight_bonds",
                {"bond_colors": bond_colors, "atom_pair_colors": pair_colors},
            )
            return _tool_ok(
                f"Bond color override set for {data['bonds_colored']} bond(s); "
                "persists across redraws until reset_cpk_color_override."
            )

        # ------------------------------------------------------------------
        # Plugin authoring helpers (run in server thread — no Qt for fetch/read)
        # ------------------------------------------------------------------

        if name == "get_plugin_dev_manual":
            manual = _fetch_plugin_dev_manual()
            return _tool_ok(manual)

        if name == "list_app_source_tree":
            path = _str_arg(arguments, "path")
            result = bridge.call("list_app_source_tree", {"path": path})
            return _tool_ok(result["content"])

        if name == "get_app_source":
            path = _str_arg(arguments, "path")
            if not path:
                return _tool_err("'path' argument is required.")
            result = bridge.call("get_app_source", {"path": path})
            content = result["content"]
            if result.get("type") != "directory":
                content = _slice_lines(
                    content, arguments.get("start_line"), arguments.get("end_line")
                )
            return _tool_ok(content)

        if name in ("grep_files", "find_files"):
            root = _str_arg(arguments, "root", "app_source")
            sub_path = _str_arg(arguments, "path")
            base, start, allowed_exts = _resolve_search_root(bridge, root, sub_path)
            if name == "find_files":
                return _tool_ok(
                    run_find(
                        start,
                        base,
                        name_glob=_str_arg(arguments, "pattern", "*"),
                        max_results=arguments.get("max_results", 200),
                    )
                )
            return _tool_ok(
                run_grep(
                    start,
                    base,
                    pattern=arguments.get("pattern", ""),
                    name_glob=_str_arg(arguments, "glob", "*.py"),
                    allowed_exts=allowed_exts,
                    ignore_case=bool(arguments.get("ignore_case", False)),
                    fixed_string=bool(arguments.get("fixed_string", False)),
                    context=arguments.get("context", 0),
                    max_matches=arguments.get("max_matches", 100),
                )
            )

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

        if name == "list_available_plugins":
            try:
                with urllib.request.urlopen(_PLUGIN_REGISTRY_URL, timeout=15) as resp:
                    entries = json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 — network errors vary widely
                return _tool_err(f"Could not fetch the plugin registry: {exc}")
            search = _str_arg(arguments, "search").lower()
            lines = []
            for entry in entries:
                if not entry.get("visible", False):
                    continue
                pname = entry.get("name", "?")
                desc = entry.get("description", "")
                tags = ", ".join(entry.get("tags", []))
                if search and search not in f"{pname} {desc} {tags}".lower():
                    continue
                lines.append(
                    f"- {pname} (v{entry.get('version', '?')})"
                    + (f" [{tags}]" if tags else "")
                    + (f"\n    {desc}" if desc else "")
                )
            if not lines:
                return _tool_ok(
                    f"No plugins in the registry match {search!r}."
                    if search
                    else "The plugin registry returned no visible plugins."
                )
            header = f"{len(lines)} plugin(s) available in the official registry"
            if search:
                header += f" matching {search!r}"
            return _tool_ok(
                header
                + ":\n"
                + "\n".join(lines)
                + "\n\nTo install one, call open_plugin_installer and let the "
                "user pick it in the installer window."
            )

        if name == "open_plugin_installer":
            data = bridge.call("open_plugin_installer")
            if data["found"]:
                return _tool_ok(
                    "Plugin Installer window opened in MoleditPy. "
                    "Ask the user to select and install the plugin there."
                )
            return _tool_err(
                "The Plugin Installer plugin is not installed in this MoleditPy. "
                "Manual install: download the plugin from the Plugin Explorer at "
                "https://hiroyokoyama.github.io/moleditpy-plugins/explorer/ and "
                "place it in the MoleditPy plugin directory (see get_plugin_dir), "
                "then call reload_plugins."
            )

        # ------------------------------------------------------------------
        # File I/O tools (sandboxed; run in server thread — no Qt needed)
        # ------------------------------------------------------------------

        if name == "get_file_io_config":
            cfg = bridge.call("get_file_io_config")
            base_dir = cfg.get("base_dir") or "(not configured)"
            exts = ", ".join(cfg.get("allowed_extensions", []))
            roots = "\n".join(f"  {r}" for r in cfg.get("read_roots", [])) or "  (none)"
            return _tool_ok(
                f"Base directory: {base_dir}\nAllowed extensions: {exts or '(none)'}\n"
                f"Read-only folders:\n{roots}"
            )

        if name == "set_file_io_config":
            args_inner: dict[str, Any] = {}
            if "base_dir" in arguments:
                bd = _str_arg(arguments, "base_dir")
                if not bd:
                    # Path("") resolves to the process's working directory,
                    # which would silently become the sandbox.
                    return _tool_err("'base_dir' must be a non-empty directory path.")
                p = Path(bd).expanduser().resolve()
                if not p.is_dir():
                    return _tool_err(
                        f"{bd!r} does not exist or is not a directory. "
                        "Create it first or provide an existing path."
                    )
                args_inner["base_dir"] = str(p)
            if "allowed_extensions" in arguments:
                args_inner["allowed_extensions"] = normalize_extensions(
                    arguments["allowed_extensions"]
                )
            if not args_inner:
                return _tool_err("Provide at least base_dir or allowed_extensions.")
            # The user approves the change in a dialog: allow minutes.
            outcome = bridge.call(
                "set_file_io_config", args_inner, timeout=APPROVAL_WAIT_SECONDS
            )
            if isinstance(outcome, dict) and outcome.get("declined"):
                return _tool_err(
                    "The user declined the file I/O change; nothing was changed. "
                    "Do not retry the same request."
                )
            parts = []
            if "base_dir" in args_inner:
                parts.append(f"Base directory: {args_inner['base_dir']}")
            if "allowed_extensions" in args_inner:
                parts.append(
                    f"Allowed extensions: {', '.join(args_inner['allowed_extensions'])}"
                )
            return _tool_ok("File I/O config updated.\n" + "\n".join(parts))

        if name == "write_text_file":
            user_path = _str_arg(arguments, "path")
            content = _text_arg(arguments.get("content", ""))
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
            return _tool_ok(f"Written: {user_path} ({size:,} bytes)")

        if name == "write_file_with_xyz_block":
            user_path = _str_arg(arguments, "path")
            if not user_path:
                return _tool_err("'path' argument is required.")
            overwrite = bool(arguments.get("overwrite", False))
            base_dir, allowed_exts = _get_sandbox(bridge)
            target = _resolve_safe_path(user_path, base_dir)
            _check_extension(target, allowed_exts)
            if target.exists() and not overwrite:
                return _tool_err(
                    f"{user_path!r} already exists. Pass overwrite=true to replace it."
                )

            data = bridge.call("get_xyz_atoms")
            if not data["has_data"]:
                return _tool_err(
                    "No 3D coordinates available. "
                    "Use trigger_3d_conversion first, or load XYZ data via show_xyz_in_viewer."
                )
            block = format_xyz_block(
                data["atoms"],
                element_style=arguments.get("element_style", "symbol"),
                atom_order=arguments.get("atom_order"),
                precision=int(arguments.get("precision", 6)),
            )
            n_atoms = (
                len(arguments["atom_order"])
                if arguments.get("atom_order")
                else len(data["atoms"])
            )

            parts = []
            if bool(arguments.get("xyz_header", False)):
                parts.append(f"{n_atoms}\n{arguments.get('comment', '')}\n")
            header = _text_arg(arguments.get("header", ""))
            if header:
                parts.append(header if header.endswith("\n") else header + "\n")
            parts.append(block + "\n")
            footer = _text_arg(arguments.get("footer", ""))
            if footer:
                parts.append(footer if footer.endswith("\n") else footer + "\n")
            content = "".join(parts)

            if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
                return _tool_err(
                    f"Content exceeds the {_MAX_FILE_BYTES // 1024 // 1024} MB limit."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            size = target.stat().st_size
            return _tool_ok(
                f"Written: {user_path} ({size:,} bytes, {n_atoms} atom(s) in coordinate block)"
            )

        if name == "read_text_file":
            user_path = _str_arg(arguments, "path")
            if not user_path:
                return _tool_err("'path' argument is required.")
            target, allowed_exts = _resolve_read_path(bridge, user_path)
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
            return _tool_ok(
                _slice_lines(
                    target.read_text(encoding="utf-8"),
                    arguments.get("start_line"),
                    arguments.get("end_line"),
                )
            )

        if name == "list_directory":
            user_path = _str_arg(arguments, "path", ".")
            target, _ = _resolve_read_path(bridge, user_path)
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
            user_path = _str_arg(arguments, "path")
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
            "Timed out waiting for MoleditPy to respond. The application may be busy."
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Tool %r raised an unhandled exception", name)
        return _tool_err(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Protocol helpers (2026-07-28 "modern" era + handshake-based "legacy" era)
# ---------------------------------------------------------------------------


def supported_versions(mode: str = "auto") -> list[str]:
    """Protocol versions this server accepts under *mode*, newest first."""
    if mode == "modern":
        return list(_MODERN_PROTOCOL_VERSIONS)
    if mode == "legacy":
        return list(_LEGACY_PROTOCOL_VERSIONS)
    return list(_MODERN_PROTOCOL_VERSIONS) + list(_LEGACY_PROTOCOL_VERSIONS)


def decode_header_value(value: str) -> str:
    """Decode the ``=?base64?...?=`` sentinel form used by header mirroring."""
    if value.startswith("=?base64?") and value.endswith("?="):
        import base64  # pylint: disable=import-outside-toplevel

        try:
            return base64.b64decode(value[9:-2]).decode("utf-8")
        except Exception:  # pylint: disable=broad-except
            return value
    return value


def is_modern_request(message: dict[str, Any], headers: dict[str, str]) -> bool:
    """
    True if *message* is framed per 2026-07-28 (per-request metadata).

    A modern client declares its protocol version in the body ``_meta`` and
    mirrors it into the ``MCP-Protocol-Version`` header; ``server/discover``
    exists only in the modern era, so it counts on its own.
    """
    if message.get("method") == "server/discover":
        return True
    params = message.get("params") or {}
    meta = params.get("_meta") or {}
    if isinstance(meta, dict) and meta.get(_META_PROTOCOL_VERSION):
        return True
    return headers.get("mcp-protocol-version", "") in _MODERN_PROTOCOL_VERSIONS


def validate_modern_request(
    message: dict[str, Any], headers: dict[str, str], mode: str = "auto"
) -> dict[str, Any] | None:
    """
    Check a modern request's version and mirrored headers.

    Returns ``None`` when the request is acceptable, otherwise a JSON-RPC
    ``error`` object (the caller sends it with HTTP 400).
    """
    method = message.get("method", "")
    params = message.get("params") or {}
    meta = params.get("_meta") or {}
    meta_version = meta.get(_META_PROTOCOL_VERSION) if isinstance(meta, dict) else None
    header_version = headers.get("mcp-protocol-version")

    # `server/discover` is how a client learns which versions exist, so a bare
    # probe that claims no version at all is answered rather than rejected.
    if (
        method == "server/discover"
        and not header_version
        and not meta_version
        and not headers.get("mcp-method")
    ):
        return None

    if not header_version:
        return {
            "code": _ERR_HEADER_MISMATCH,
            "message": "Header mismatch: required header 'MCP-Protocol-Version' is missing.",
        }
    if meta_version and meta_version != header_version:
        return {
            "code": _ERR_HEADER_MISMATCH,
            "message": (
                f"Header mismatch: MCP-Protocol-Version header {header_version!r} "
                f"does not match body _meta value {meta_version!r}."
            ),
        }
    requested = meta_version or header_version
    allowed = supported_versions(mode)
    if requested not in allowed:
        return {
            "code": _ERR_UNSUPPORTED_PROTOCOL_VERSION,
            "message": "Unsupported protocol version",
            "data": {"supported": allowed, "requested": requested},
        }

    header_method = headers.get("mcp-method")
    if not header_method:
        return {
            "code": _ERR_HEADER_MISMATCH,
            "message": "Header mismatch: required header 'Mcp-Method' is missing.",
        }
    if header_method != method:
        return {
            "code": _ERR_HEADER_MISMATCH,
            "message": (
                f"Header mismatch: Mcp-Method header {header_method!r} does not "
                f"match body method {method!r}."
            ),
        }

    if method == "tools/call":
        header_name = headers.get("mcp-name")
        if not header_name:
            return {
                "code": _ERR_HEADER_MISMATCH,
                "message": (
                    "Header mismatch: required header 'Mcp-Name' is missing "
                    "for tools/call."
                ),
            }
        body_name = params.get("name", "")
        if decode_header_value(header_name) != body_name:
            return {
                "code": _ERR_HEADER_MISMATCH,
                "message": (
                    f"Header mismatch: Mcp-Name header {header_name!r} does not "
                    f"match body params.name {body_name!r}."
                ),
            }
    return None


def build_discover_result(
    mode: str, server_name: str, server_version: str
) -> dict[str, Any]:
    """Build the 2026-07-28 ``server/discover`` result."""
    return {
        "supportedVersions": supported_versions(mode),
        "capabilities": {"tools": {}},
        "instructions": _SERVER_INSTRUCTIONS,
        "ttlMs": _DISCOVER_TTL_MS,
        "cacheScope": "private",
        "_meta": {_META_SERVER_INFO: {"name": server_name, "version": server_version}},
    }


def negotiate_legacy_version(requested: Any) -> str:
    """Echo the client's legacy protocol version when we speak it."""
    if isinstance(requested, str) and requested in _LEGACY_PROTOCOL_VERSIONS:
        return requested
    return _PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

#: Largest request body accepted. write_text_file caps content at 4 MB and
#: JSON escaping can roughly double that, so this leaves headroom without
#: letting one request exhaust memory.
_MAX_BODY_BYTES = 16 * 1024 * 1024

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(host_header: str) -> str:
    """The host part of a ``Host`` header value: ``[::1]:7891`` -> ``::1``."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_loopback_origin(origin: str) -> bool:
    """True for an ``Origin`` served from this machine (any port or scheme)."""
    try:
        parsed = urllib.parse.urlsplit(origin)
        hostname = parsed.hostname or ""
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and hostname in _LOOPBACK_HOSTS


class _MCPHandler(BaseHTTPRequestHandler):
    """HTTP request handler implementing MCP Streamable HTTP transport."""

    # Class-level defaults. The live values are per-server-instance (set on
    # the HTTPServer object by MCPHttpServer.start) so two servers in one
    # process — e.g. one per protocol mode — never overwrite each other.
    bridge: Any = None
    server_name: str = "MoleditPy MCP Server"
    server_version: str = "unknown"
    session_id: str = ""
    protocol_mode: str = "auto"

    def log_message(self, format_str: str, *args: Any) -> None:  # type: ignore[override]
        logger.debug(format_str, *args)

    def _cfg(self, name: str) -> Any:
        """Read per-server config, falling back to the class default."""
        return getattr(getattr(self, "server", None), f"mcp_{name}", None) or getattr(
            type(self), name
        )

    def _header(self, name: str) -> str | None:
        """Case-insensitive request header lookup (None when absent)."""
        headers = getattr(self, "headers", None)
        if not headers:
            return None
        wanted = name.lower()
        for key, value in headers.items():
            if key.lower() == wanted:
                return value
        return None

    # ------------------------------------------------------------------
    # Origin / Host checks and CORS
    # ------------------------------------------------------------------

    def _request_is_local(self) -> bool:
        """Refuse requests a web page could have made.

        This endpoint runs arbitrary Python (run_python) and writes files, so
        a browser tab must not be able to drive it. Any page can POST to
        127.0.0.1, and a DNS-rebinding page can even read the reply, so a
        browser request's ``Origin`` must be this machine and the ``Host``
        must name the address the server is bound to. Native MCP clients
        send no ``Origin`` and are unaffected.
        """
        origin = self._header("Origin")
        if origin is not None and not is_loopback_origin(origin):
            logger.warning("Rejected MCP request from origin %r", origin)
            return False
        host = self._header("Host")
        server_address = getattr(getattr(self, "server", None), "server_address", None)
        if host and server_address:
            allowed = set(_LOOPBACK_HOSTS) | {str(server_address[0]).lower()}
            if _hostname(host) not in allowed:
                logger.warning("Rejected MCP request for host %r", host)
                return False
        return True

    def _send_cors(self) -> None:
        # Echo a loopback Origin (e.g. a local MCP Inspector) rather than
        # "*": a wildcard would let any web page read the responses.
        origin = self._header("Origin")
        if origin is not None and is_loopback_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Accept, Mcp-Session-Id, "
            "MCP-Protocol-Version, Mcp-Method, Mcp-Name",
        )
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id")

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # pylint: disable=invalid-name
        if not self._request_is_local():
            self.send_error(403, "Forbidden origin")
            return
        self.send_response(200)
        self._send_cors()
        self.end_headers()

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        if self.path in ("/", "/health"):
            body = json.dumps(
                {
                    "status": "ok",
                    "server": self._cfg("server_name"),
                    "version": self._cfg("server_version"),
                    "protocolMode": self._cfg("protocol_mode"),
                    "supportedVersions": supported_versions(self._cfg("protocol_mode")),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._send_cors()
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/mcp":
            # 2026-07-28 has no standalone SSE stream; older revisions used
            # GET for it, so answer the way the spec prescribes.
            self.send_error(405, "GET is not supported on the MCP endpoint")
        else:
            self.send_error(404)

    def do_DELETE(self) -> None:  # pylint: disable=invalid-name
        if self.path == "/mcp":
            self.send_error(405, "Sessions are not used; nothing to delete")
        else:
            self.send_error(404)

    def _content_length(self) -> int | None:
        """The declared body length, or None when it is malformed."""
        try:
            length = int(self._header("Content-Length") or 0)
        except ValueError:
            return None
        return length if length >= 0 else None

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        length = self._content_length()
        if self.path != "/mcp":
            # Read the body before answering: closing a socket with unread
            # data makes Windows reset the connection, so the client sees
            # ConnectionAbortedError instead of this 404.
            if length and length <= _MAX_BODY_BYTES:
                self.rfile.read(length)
            self.send_error(404, "Use POST /mcp")
            return
        if not self._request_is_local():
            self.send_error(403, "Forbidden origin")
            return
        if length is None:
            self.send_error(400, "Invalid Content-Length")
            return
        if length == 0:
            self.send_error(400, "Empty body")
            return
        if length > _MAX_BODY_BYTES:
            self.send_error(413, "Request body too large")
            return
        try:
            raw = self.rfile.read(length)
            message = json.loads(raw)
        except (ValueError, OSError) as exc:  # JSONDecodeError, UnicodeDecodeError
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32700, "message": str(exc)},
                    "id": None,
                }
            )
            return
        if not isinstance(message, dict) or not isinstance(
            message.get("params") or {}, dict
        ):
            # Batches were removed from MCP (2025-06-18); a bare value or a
            # non-object params is not a request this server can route.
            self._send_json(
                {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32600,
                        "message": "Invalid Request: expected a JSON-RPC object",
                    },
                    "id": message.get("id") if isinstance(message, dict) else None,
                },
                status=400,
            )
            return
        self._process(message)

    # ------------------------------------------------------------------
    # MCP JSON-RPC processing
    # ------------------------------------------------------------------

    def _process(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        method = message.get("method", "")
        params: dict[str, Any] = message.get("params") or {}
        raw_headers = getattr(self, "headers", None)
        headers = {k.lower(): v for k, v in raw_headers.items()} if raw_headers else {}
        modern = is_modern_request(message, headers)

        # Notifications (no id) — acknowledge with 202
        if msg_id is None:
            logger.debug("MCP notification: %s", method)
            self.send_response(202)
            self._send_cors()
            self.end_headers()
            return

        if modern:
            if self._cfg("protocol_mode") == "legacy":
                self._send_error(
                    msg_id,
                    {
                        "code": _ERR_UNSUPPORTED_PROTOCOL_VERSION,
                        "message": "Unsupported protocol version",
                        "data": {
                            "supported": supported_versions("legacy"),
                            "requested": headers.get(
                                "mcp-protocol-version", _MODERN_PROTOCOL_VERSION
                            ),
                        },
                    },
                    status=400,
                    modern=True,
                )
                return
            error = validate_modern_request(
                message, headers, self._cfg("protocol_mode")
            )
            if error is not None:
                self._send_error(msg_id, error, status=400, modern=True)
                return
        elif self._cfg("protocol_mode") == "modern":
            # A handshake-based client cannot fall forward on its own, so name
            # the versions we do speak in the error it will surface.
            self._send_error(
                msg_id,
                {
                    "code": _ERR_METHOD_NOT_FOUND,
                    "message": (
                        f"This server is configured for MCP "
                        f"{_MODERN_PROTOCOL_VERSION} only and does not implement "
                        f"the '{method}' handshake. Supported versions: "
                        f"{', '.join(supported_versions('modern'))}."
                    ),
                },
                status=404,
                modern=False,
            )
            return

        try:
            result = self._handle_method(method, params, modern)
        except _MethodNotFound:
            self._send_error(
                msg_id,
                {
                    "code": _ERR_METHOD_NOT_FOUND,
                    "message": f"Method not found: {method}",
                },
                status=404 if modern else 200,
                modern=modern,
            )
            return
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Unhandled error processing %r", method)
            self._send_error(
                msg_id,
                {"code": -32603, "message": f"Internal error: {exc}"},
                status=200,
                modern=modern,
            )
            return

        self._send_json(
            {"jsonrpc": "2.0", "result": result, "id": msg_id}, modern=modern
        )

    def _handle_method(
        self, method: str, params: dict[str, Any], modern: bool = False
    ) -> Any:
        if method == "server/discover":
            return build_discover_result(
                self._cfg("protocol_mode"),
                self._cfg("server_name"),
                self._cfg("server_version"),
            )
        if method == "initialize":
            return {
                "protocolVersion": negotiate_legacy_version(
                    params.get("protocolVersion")
                ),
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": self._cfg("server_name"),
                    "version": self._cfg("server_version"),
                },
                "instructions": _SERVER_INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            result: dict[str, Any] = {"tools": _TOOLS}
            if modern:
                result["ttlMs"] = _TOOLS_TTL_MS
                result["cacheScope"] = "private"
            return result
        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments: dict[str, Any] = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _tool_err("'arguments' must be a JSON object.")
            if self._cfg("bridge") is None:
                return _tool_err("Bridge not initialized.")
            return dispatch_tool(self._cfg("bridge"), tool_name, arguments)
        raise _MethodNotFound(method)

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _send_error(
        self,
        msg_id: Any,
        error: dict[str, Any],
        status: int = 200,
        modern: bool = False,
    ) -> None:
        self._send_json(
            {"jsonrpc": "2.0", "error": error, "id": msg_id},
            status=status,
            modern=modern,
        )

    def _send_json(
        self, data: dict[str, Any], status: int = 200, modern: bool = False
    ) -> None:
        if modern and "result" in data and isinstance(data["result"], dict):
            data["result"].setdefault("resultType", _RESULT_TYPE_COMPLETE)
            meta = data["result"].setdefault("_meta", {})
            meta.setdefault(
                _META_SERVER_INFO,
                {
                    "name": self._cfg("server_name"),
                    "version": self._cfg("server_version"),
                },
            )
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if not modern:
            # Sessions exist only in the handshake era; 2026-07-28 servers
            # must not mint or echo a session id.
            self.send_header("Mcp-Session-Id", self._cfg("session_id"))
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
    # SO_REUSEADDR means "may share a port in use" on Windows, not "may reuse
    # one in TIME_WAIT": a second MoleditPy would bind the same port without
    # error and the two would split the traffic. Windows gets an exclusive
    # bind instead, so a busy port fails loudly at start().
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class MCPHttpServer:
    """Manages the lifecycle of the background MCP HTTP server thread."""

    def __init__(
        self,
        bridge: Any,
        server_name: str,
        server_version: str,
        host: str = "127.0.0.1",
        port: int = 7891,
        protocol_mode: str = "auto",
    ) -> None:
        self._bridge = bridge
        self._server_name = server_name
        self._server_version = server_version
        self._host = host
        self._port = port
        self._protocol_mode = (
            protocol_mode if protocol_mode in PROTOCOL_MODES else "auto"
        )
        self._httpd: _ThreadedHTTPServer | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the HTTP server in a daemon thread."""
        self._httpd = _ThreadedHTTPServer((self._host, self._port), _MCPHandler)
        self._httpd.mcp_bridge = self._bridge
        self._httpd.mcp_server_name = self._server_name
        self._httpd.mcp_server_version = self._server_version
        self._httpd.mcp_session_id = uuid.uuid4().hex
        self._httpd.mcp_protocol_mode = self._protocol_mode
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
            # shutdown() only stops the serve_forever() loop; the listening
            # socket itself must be closed explicitly or its file descriptor
            # leaks (e.g. across repeated start/stop cycles in the UI).
            self._httpd.server_close()
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

    @property
    def protocol_mode(self) -> str:
        return self._protocol_mode

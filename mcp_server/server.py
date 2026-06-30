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
import socketserver
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
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

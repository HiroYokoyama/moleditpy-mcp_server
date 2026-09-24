"""
Regression tests for the robustness/security pass: requests a browser could
make, malformed JSON-RPC, bad argument types, port sharing, search roots under
a virtualenv, and RDKit results that are not JSON-safe.
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from conftest import load_module, make_bridge, mock_optional_imports
from test_bridge import _real_bridge


@pytest.fixture()
def srv():
    with mock_optional_imports():
        yield load_module("server.py")


# ---------------------------------------------------------------------------
# Live server over a real socket
# ---------------------------------------------------------------------------


class _StubBridge:
    def call(
        self, operation: str, args: dict | None = None, timeout: float = 10.0
    ) -> Any:
        if operation == "get_app_info":
            return {"app": "MoleditPy", "version": "t", "mcp_plugin_version": "t"}
        raise ValueError(f"unexpected {operation}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live():
    from mcp_server.server import MCPHttpServer

    server = MCPHttpServer(_StubBridge(), "Stub", "0", port=_free_port())
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _post(
    port: int, body: bytes, headers: dict[str, str] | None = None, path: str = "/mcp"
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


_PING = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()


def test_foreign_origin_is_refused(live):
    """A web page must not be able to drive run_python / file writes."""
    status, _, _ = _post(live.port, _PING, {"Origin": "https://evil.example"})
    assert status == 403


def test_opaque_null_origin_is_refused(live):
    status, _, _ = _post(live.port, _PING, {"Origin": "null"})
    assert status == 403


def test_loopback_origin_is_allowed_and_echoed(live):
    status, headers, body = _post(live.port, _PING, {"Origin": "http://localhost:6274"})
    assert status == 200
    assert json.loads(body)["result"] == {}
    assert headers["access-control-allow-origin"] == "http://localhost:6274"


def test_native_client_without_origin_gets_no_wildcard_cors(live):
    status, headers, _ = _post(live.port, _PING)
    assert status == 200
    assert "access-control-allow-origin" not in headers


def test_preflight_from_foreign_origin_is_refused(live):
    conn = http.client.HTTPConnection("127.0.0.1", live.port, timeout=5)
    try:
        conn.request("OPTIONS", "/mcp", headers={"Origin": "https://evil.example"})
        assert conn.getresponse().status == 403
    finally:
        conn.close()


def test_dns_rebinding_host_is_refused(live):
    """A rebinding page reaches 127.0.0.1 but still sends its own Host."""
    status, _, _ = _post(live.port, _PING, {"Host": f"evil.example:{live.port}"})
    assert status == 403


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_host_headers_are_accepted(live, host):
    status, _, _ = _post(live.port, _PING, {"Host": f"{host}:{live.port}"})
    assert status == 200


def test_wrong_path_post_is_a_clean_404(live):
    """The body is drained first, so Windows does not reset the connection."""
    for _ in range(20):
        status, _, _ = _post(live.port, _PING * 50, path="/nope")
        assert status == 404


def test_batch_request_is_invalid_request(live):
    status, _, body = _post(
        live.port, json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).encode()
    )
    assert status == 400
    assert json.loads(body)["error"]["code"] == -32600


def test_non_object_params_is_invalid_request(live):
    msg = {"jsonrpc": "2.0", "id": 4, "method": "ping", "params": [1, 2]}
    status, _, body = _post(live.port, json.dumps(msg).encode())
    assert status == 400
    data = json.loads(body)
    assert data["error"]["code"] == -32600
    assert data["id"] == 4


def test_non_utf8_body_is_a_parse_error(live):
    status, _, body = _post(live.port, b"\xff\xfe{")
    assert status == 200
    assert json.loads(body)["error"]["code"] == -32700


def test_non_object_tool_arguments_is_a_tool_error(live):
    msg = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "get_app_info", "arguments": ["x"]},
    }
    _, _, body = _post(live.port, json.dumps(msg).encode())
    result = json.loads(body)["result"]
    assert result["isError"] is True
    assert "must be a JSON object" in result["content"][0]["text"]


def test_second_server_on_same_port_fails_to_start(live):
    """On Windows SO_REUSEADDR let two servers share one port silently."""
    from mcp_server.server import MCPHttpServer

    second = MCPHttpServer(_StubBridge(), "Stub", "0", port=live.port)
    with pytest.raises(OSError):
        second.start()


# ---------------------------------------------------------------------------
# Handler-level edge cases
# ---------------------------------------------------------------------------


def _bare_handler(srv_mod: Any, headers: dict[str, str]) -> Any:
    from unittest.mock import MagicMock

    handler = object.__new__(srv_mod._MCPHandler)
    handler.path = "/mcp"
    handler.headers = headers
    handler.rfile = MagicMock()
    handler.send_error = MagicMock()
    return handler


def test_oversized_body_is_refused_without_reading(srv):
    handler = _bare_handler(srv, {"Content-Length": str(srv._MAX_BODY_BYTES + 1)})
    handler.do_POST()
    handler.send_error.assert_called_once_with(413, "Request body too large")
    handler.rfile.read.assert_not_called()


@pytest.mark.parametrize("value", ["abc", "-5"])
def test_malformed_content_length_is_400(srv, value):
    handler = _bare_handler(srv, {"Content-Length": value})
    handler.do_POST()
    handler.send_error.assert_called_once_with(400, "Invalid Content-Length")


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("http://127.0.0.1:8080", True),
        ("https://localhost", True),
        ("http://[::1]:3000", True),
        ("http://localhost.evil.example", False),
        ("file://", False),
        ("null", False),
        ("http://[bad", False),
    ],
)
def test_is_loopback_origin(srv, origin, expected):
    assert srv.is_loopback_origin(origin) is expected


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, 42])
def test_null_or_non_string_smiles_is_a_clean_tool_error(srv, value):
    bridge = make_bridge({})
    result = srv.dispatch_tool(bridge, "load_molecule_from_smiles", {"smiles": value})
    assert result["isError"] is True
    assert "AttributeError" not in result["content"][0]["text"]
    bridge.call.assert_not_called()


def test_blank_root_falls_back_to_default(srv, tmp_path):
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    bridge = make_bridge({"get_app_source_root": {"root": str(tmp_path)}})
    result = srv.dispatch_tool(
        bridge, "grep_files", {"pattern": "needle", "root": "  "}
    )
    assert "a.py:1:" in result["content"][0]["text"]


def test_set_file_io_config_rejects_a_bare_string_extension_list(srv):
    bridge = make_bridge({"set_file_io_config": {"success": True}})
    result = srv.dispatch_tool(
        bridge, "set_file_io_config", {"allowed_extensions": ".inp"}
    )
    assert result["isError"] is True
    bridge.call.assert_not_called()


def test_set_file_io_config_rejects_empty_base_dir(srv):
    """Path('') resolves to the working directory, which must not become the sandbox."""
    bridge = make_bridge({"set_file_io_config": {"success": True}})
    result = srv.dispatch_tool(bridge, "set_file_io_config", {"base_dir": ""})
    assert result["isError"] is True
    bridge.call.assert_not_called()


def test_set_file_io_config_normalizes_extensions(srv):
    bridge = make_bridge({"set_file_io_config": {"success": True}})
    srv.dispatch_tool(
        bridge, "set_file_io_config", {"allowed_extensions": ["INP", ".xyz", "inp"]}
    )
    _, args = bridge.call.call_args[0]
    assert args["allowed_extensions"] == [".inp", ".xyz"]


# ---------------------------------------------------------------------------
# Search roots
# ---------------------------------------------------------------------------


def test_search_works_when_the_root_lives_inside_a_venv(srv, tmp_path):
    """The skip list applies below the root, not to the root's own path."""
    root = tmp_path / ".venv" / "Lib" / "site-packages" / "moleditpy"
    (root / "core").mkdir(parents=True)
    (root / "core" / "mol.py").write_text("def needle():\n    pass\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "mol.py").write_text("needle\n", encoding="utf-8")

    grep = srv.run_grep(root, root, "needle")
    assert "core/mol.py:1:" in grep
    assert "__pycache__" not in grep
    assert "core/mol.py" in srv.run_find(root, root, "*.py")


# ---------------------------------------------------------------------------
# RDKit results (real RDKit)
# ---------------------------------------------------------------------------


def _ctx(smiles: str, embed: bool = False) -> tuple[Any, Any]:
    from unittest.mock import MagicMock

    pytest.importorskip("rdkit")
    from rdkit import Chem
    from rdkit.Chem import AllChem

    mod = _real_bridge()
    mol = Chem.MolFromSmiles(smiles)
    if embed:
        mol = Chem.AddHs(mol)
        assert AllChem.EmbedMolecule(mol, randomSeed=7) == 0
    ctx = MagicMock()
    ctx.current_molecule = mol
    return mod, ctx


def test_partial_charges_without_parameters_are_json_safe():
    """Gasteiger gives NaN for Sn; NaN would make the whole reply invalid JSON."""
    mod, ctx = _ctx("CC[Sn](CC)(CC)CC")
    charges = mod.execute_operation(ctx, "compute_partial_charges", {})["charges"]
    assert all(c["charge"] is None for c in charges)
    json.dumps(charges, allow_nan=False)


def test_partial_charges_n_a_is_rendered(srv):
    bridge = make_bridge(
        {
            "compute_partial_charges": {
                "charges": [{"index": 0, "symbol": "Sn", "charge": None}]
            }
        }
    )
    text = srv.dispatch_tool(bridge, "compute_partial_charges", {})["content"][0][
        "text"
    ]
    assert "n/a" in text


def test_optimize_without_force_field_parameters_errors_instead_of_succeeding():
    mod, ctx = _ctx("CC[Sn](CC)(CC)CC", embed=True)
    before = ctx.current_molecule
    with pytest.raises(ValueError, match="could not be set up"):
        mod.execute_operation(ctx, "optimize_geometry", {"force_field": "mmff"})
    assert ctx.current_molecule is before
    ctx.push_undo_checkpoint.assert_not_called()


def test_atom_properties_out_of_range_index_is_explained():
    mod, ctx = _ctx("CCO")
    with pytest.raises(ValueError, match=r"atom_index 9 is out of range \(0-2\)"):
        mod.execute_operation(ctx, "get_atom_properties", {"atom_indices": [9]})


def test_highlight_atoms_bad_key_applies_nothing():
    from unittest.mock import MagicMock

    mod = _real_bridge()
    ctx = MagicMock()
    with pytest.raises(ValueError, match="Invalid atom index 'x'"):
        mod.execute_operation(
            ctx, "highlight_atoms", {"atom_colors": {"0": "#F00", "x": "#0F0"}}
        )
    ctx.get_3d_controller.return_value.set_atom_color.assert_not_called()


def test_reaction_that_matches_but_breaks_valence_is_not_called_a_non_match():
    """[H] matches only on the explicit-H attempt; its product is over-valent."""
    mod, ctx = _ctx("CC")
    with pytest.raises(ValueError, match="invalid molecule"):
        mod.execute_operation(
            ctx,
            "apply_reaction_smarts",
            {"reaction_smarts": "[C:1][H]>>[C:1](F)(F)(F)F", "convert_to_3d": False},
        )


def test_ui_placeholder_no_longer_claims_unrestricted():
    source = (Path(__file__).resolve().parents[1] / "mcp_server" / "ui.py").read_text(
        "utf-8"
    )
    assert "(unrestricted)" not in source

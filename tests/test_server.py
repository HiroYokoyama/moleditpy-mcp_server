"""Tests for mcp_server/server.py — MCP protocol and tool dispatch."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from conftest import load_module, make_bridge, mock_optional_imports


@pytest.fixture()
def srv():
    """Load server.py in isolation with deps mocked."""
    with mock_optional_imports():
        yield load_module("server.py")


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def test_tools_list_nonempty(srv):
    assert len(srv._TOOLS) >= 7


def test_every_tool_has_required_fields(srv):
    for tool in srv._TOOLS:
        assert "name" in tool, f"{tool} missing 'name'"
        assert "description" in tool, f"{tool} missing 'description'"
        assert "inputSchema" in tool, f"{tool} missing 'inputSchema'"
        schema = tool["inputSchema"]
        assert schema.get("type") == "object"


def test_tool_names_are_unique(srv):
    names = [t["name"] for t in srv._TOOLS]
    assert len(names) == len(set(names)), "Duplicate tool name detected"


def test_known_tools_present(srv):
    names = {t["name"] for t in srv._TOOLS}
    expected = {
        "get_current_molecule",
        "get_molecule_xyz",
        "load_molecule_from_smiles",
        "show_xyz_in_viewer",
        "get_selected_atoms",
        "clear_canvas",
        "get_app_info",
    }
    assert expected <= names


def test_required_tools_have_required_schema_fields(srv):
    load_tool = next(
        t for t in srv._TOOLS if t["name"] == "load_molecule_from_smiles"
    )
    assert "required" in load_tool["inputSchema"]
    assert "smiles" in load_tool["inputSchema"]["required"]


# ---------------------------------------------------------------------------
# Tool result helpers
# ---------------------------------------------------------------------------


def test_tool_ok_structure(srv):
    result = srv._tool_ok("hello")
    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == "hello"
    assert "isError" not in result


def test_tool_err_structure(srv):
    result = srv._tool_err("oops")
    assert result["content"][0]["text"] == "oops"
    assert result["isError"] is True


# ---------------------------------------------------------------------------
# dispatch_tool
# ---------------------------------------------------------------------------


def _bridge(results: dict) -> MagicMock:
    return make_bridge(results)


def test_dispatch_get_current_molecule_no_mol(srv):
    bridge = _bridge({"get_molecule_info": {"loaded": False}})
    result = srv.dispatch_tool(bridge, "get_current_molecule", {})
    assert "No molecule" in result["content"][0]["text"]
    assert result.get("isError") is not True


def test_dispatch_get_current_molecule_with_mol(srv):
    bridge = _bridge({
        "get_molecule_info": {
            "loaded": True,
            "smiles": "CCO",
            "formula": "C2H6O",
            "molecular_weight": 46.0684,
            "num_atoms": 3,
            "num_bonds": 2,
            "has_3d_coords": False,
        }
    })
    result = srv.dispatch_tool(bridge, "get_current_molecule", {})
    text = result["content"][0]["text"]
    assert "CCO" in text
    assert "C2H6O" in text
    assert "46.0684" in text


def test_dispatch_get_molecule_xyz_no_data(srv):
    bridge = _bridge({"get_xyz_block": {"has_data": False, "xyz_block": None}})
    result = srv.dispatch_tool(bridge, "get_molecule_xyz", {})
    assert "No 3D" in result["content"][0]["text"]


def test_dispatch_get_molecule_xyz_with_data(srv):
    xyz = "C  0.000  0.000  0.000\nH  0.634  0.634  0.634"
    bridge = _bridge({"get_xyz_block": {"has_data": True, "xyz_block": xyz}})
    result = srv.dispatch_tool(bridge, "get_molecule_xyz", {})
    assert xyz in result["content"][0]["text"]


def test_dispatch_get_atom_properties_empty(srv):
    bridge = _bridge({"get_atom_properties": {"atoms": []}})
    result = srv.dispatch_tool(bridge, "get_atom_properties", {})
    assert "No molecule loaded or no atoms found" in result["content"][0]["text"]


def test_dispatch_get_atom_properties_with_atoms(srv):
    bridge = _bridge({
        "get_atom_properties": {
            "atoms": [
                {
                    "index": 0,
                    "symbol": "C",
                    "atomic_num": 6,
                    "formal_charge": 0,
                    "hybridization": "SP3",
                    "total_hs": 4,
                    "num_radical_electrons": 0,
                }
            ]
        }
    })
    result = srv.dispatch_tool(bridge, "get_atom_properties", {"atom_indices": [0]})
    text = result["content"][0]["text"]
    assert "C" in text
    assert "Z=6" in text
    bridge.call.assert_called_with("get_atom_properties", {"atom_indices": [0]})


def test_dispatch_get_bond_info_empty(srv):
    bridge = _bridge({"get_bond_info": {"bonds": []}})
    result = srv.dispatch_tool(bridge, "get_bond_info", {})
    assert "no bonds" in result["content"][0]["text"].lower()


def test_dispatch_get_bond_info_with_bonds(srv):
    bridge = _bridge({
        "get_bond_info": {
            "bonds": [{"index": 0, "atom1": 0, "atom2": 1, "bond_type": "DOUBLE"}]
        }
    })
    result = srv.dispatch_tool(bridge, "get_bond_info", {})
    text = result["content"][0]["text"]
    assert "DOUBLE" in text
    assert "atom 0" in text


def test_dispatch_load_smiles_missing_arg(srv):
    bridge = MagicMock()
    result = srv.dispatch_tool(bridge, "load_molecule_from_smiles", {})
    assert result.get("isError") is True
    bridge.call.assert_not_called()


def test_dispatch_load_smiles_whitespace_only(srv):
    bridge = MagicMock()
    result = srv.dispatch_tool(bridge, "load_molecule_from_smiles", {"smiles": "  "})
    assert result.get("isError") is True


def test_dispatch_load_smiles_ok(srv):
    bridge = _bridge({"load_smiles": {"success": True}})
    result = srv.dispatch_tool(
        bridge, "load_molecule_from_smiles", {"smiles": "c1ccccc1"}
    )
    assert result.get("isError") is not True
    bridge.call.assert_called_with("load_smiles", {"smiles": "c1ccccc1"})


def test_dispatch_show_xyz_missing_arg(srv):
    bridge = MagicMock()
    result = srv.dispatch_tool(bridge, "show_xyz_in_viewer", {})
    assert result.get("isError") is True


def test_dispatch_show_xyz_success(srv):
    bridge = _bridge({"show_xyz": {"success": True}})
    result = srv.dispatch_tool(
        bridge,
        "show_xyz_in_viewer",
        {"xyz_text": "C 0 0 0", "source_name": "test"},
    )
    assert "3D viewer" in result["content"][0]["text"]


def test_dispatch_show_xyz_failure(srv):
    bridge = _bridge({"show_xyz": {"success": False}})
    result = srv.dispatch_tool(
        bridge, "show_xyz_in_viewer", {"xyz_text": "bad data"}
    )
    assert result.get("isError") is True


def test_dispatch_load_from_mol_block_missing_arg(srv):
    bridge = MagicMock()
    result = srv.dispatch_tool(bridge, "load_from_mol_block", {})
    assert result.get("isError") is True
    bridge.call.assert_not_called()


def test_dispatch_load_from_mol_block_parse_failure(srv):
    bridge = _bridge({"load_mol_block": {"success": False}})
    result = srv.dispatch_tool(bridge, "load_from_mol_block", {"mol_block": "garbage"})
    assert result.get("isError") is True
    assert "Failed to parse" in result["content"][0]["text"]


def test_dispatch_trigger_3d_conversion(srv):
    bridge = _bridge({"trigger_3d_conversion": {"success": True}})
    result = srv.dispatch_tool(bridge, "trigger_3d_conversion", {})
    assert result.get("isError") is not True
    bridge.call.assert_called_with("trigger_3d_conversion", timeout=60.0)


def test_dispatch_get_selected_atoms_empty(srv):
    bridge = _bridge({"get_selected_atoms": {"count": 0, "selected_atoms": []}})
    result = srv.dispatch_tool(bridge, "get_selected_atoms", {})
    assert "No atoms" in result["content"][0]["text"]


def test_dispatch_get_selected_atoms_with_selection(srv):
    bridge = _bridge({
        "get_selected_atoms": {
            "count": 2,
            "selected_atoms": [
                {"index": 0, "symbol": "C", "atomic_num": 6},
                {"index": 3, "symbol": "O", "atomic_num": 8},
            ],
        }
    })
    result = srv.dispatch_tool(bridge, "get_selected_atoms", {})
    text = result["content"][0]["text"]
    assert "2 atom" in text
    assert "C" in text
    assert "O" in text


def test_dispatch_clear_canvas(srv):
    bridge = _bridge({"clear_canvas": {"success": True}})
    result = srv.dispatch_tool(bridge, "clear_canvas", {})
    assert "cleared" in result["content"][0]["text"].lower()
    bridge.call.assert_called_with("clear_canvas")


def test_dispatch_get_app_info(srv):
    bridge = _bridge({
        "get_app_info": {
            "app": "MoleditPy",
            "version": "4.0.0",
            "mcp_plugin_version": "2026.06.30",
        }
    })
    result = srv.dispatch_tool(bridge, "get_app_info", {})
    text = result["content"][0]["text"]
    assert "MoleditPy" in text
    assert "4.0.0" in text


def test_dispatch_unknown_tool(srv):
    bridge = MagicMock()
    result = srv.dispatch_tool(bridge, "nonexistent_tool_xyz", {})
    assert result.get("isError") is True
    assert "Unknown tool" in result["content"][0]["text"]


def test_dispatch_bridge_timeout(srv):
    bridge = MagicMock()
    bridge.call.side_effect = TimeoutError("timed out")
    result = srv.dispatch_tool(bridge, "get_current_molecule", {})
    assert result.get("isError") is True
    assert "Timed out" in result["content"][0]["text"]


def test_dispatch_bridge_exception(srv):
    bridge = MagicMock()
    bridge.call.side_effect = RuntimeError("boom")
    result = srv.dispatch_tool(bridge, "get_current_molecule", {})
    assert result.get("isError") is True
    assert "Error" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# MCPHttpServer
# ---------------------------------------------------------------------------


def test_mcp_http_server_url(srv):
    bridge = MagicMock()
    server = srv.MCPHttpServer(
        bridge, server_name="Test", server_version="1.0", host="127.0.0.1", port=19876
    )
    assert server.url == "http://127.0.0.1:19876/mcp"
    assert not server.is_running


def test_mcp_http_server_start_stop(srv):
    bridge = MagicMock()
    server = srv.MCPHttpServer(
        bridge, server_name="Test", server_version="1.0", port=0
    )
    server.start()
    assert server.is_running
    server.stop()
    assert not server.is_running


def test_mcp_http_server_stop_idempotent(srv):
    bridge = MagicMock()
    server = srv.MCPHttpServer(
        bridge, server_name="Test", server_version="1.0", port=0
    )
    server.stop()  # should not raise when not running
    assert not server.is_running


def test_mcp_http_server_stop_closes_socket(srv):
    """stop() must release the listening socket, not just stop serve_forever().

    Regression test: stop() previously called shutdown() but never
    server_close(), leaking the socket file descriptor. On a fixed port,
    a leaked socket makes an immediate restart on the same port flaky/fail
    even with allow_reuse_address; closing it lets start/stop cycle cleanly.
    """
    bridge = MagicMock()
    server = srv.MCPHttpServer(
        bridge, server_name="Test", server_version="1.0", port=0
    )
    server.start()
    httpd = server._httpd  # noqa: SLF001
    assert httpd is not None
    sock = httpd.socket
    assert sock.fileno() != -1
    server.stop()
    # A closed socket's fileno() reliably reports -1 (cross-platform, unlike
    # os.fstat() which doesn't work on Windows socket handles).
    assert sock.fileno() == -1


def test_mcp_http_server_port_property(srv):
    bridge = MagicMock()
    server = srv.MCPHttpServer(
        bridge, server_name="Test", server_version="1.0", port=12345
    )
    assert server.port == 12345


# ---------------------------------------------------------------------------
# _MCPHandler protocol (via _handle_method)
# ---------------------------------------------------------------------------


def _make_handler(srv_mod: Any) -> Any:
    """Instantiate _MCPHandler without a real HTTP request."""
    cls = srv_mod._MCPHandler
    cls.bridge = MagicMock()
    cls.session_id = "test-session"
    cls.server_name = "Test Server"
    cls.server_version = "1.0"
    handler = object.__new__(cls)
    return handler


def test_handle_initialize(srv):
    handler = _make_handler(srv)
    result = handler._handle_method("initialize", {})
    assert result["protocolVersion"] == srv._PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "Test Server"
    assert "tools" in result["capabilities"]


def test_handle_ping(srv):
    handler = _make_handler(srv)
    result = handler._handle_method("ping", {})
    assert result == {}


def test_handle_tools_list(srv):
    handler = _make_handler(srv)
    result = handler._handle_method("tools/list", {})
    assert "tools" in result
    assert len(result["tools"]) == len(srv._TOOLS)


def test_handle_tools_call_dispatches(srv):
    handler = _make_handler(srv)
    handler.__class__.bridge = make_bridge(
        {"get_molecule_info": {"loaded": False}}
    )
    result = handler._handle_method(
        "tools/call",
        {"name": "get_current_molecule", "arguments": {}},
    )
    assert "content" in result


def test_handle_unknown_method_raises(srv):
    handler = _make_handler(srv)
    with pytest.raises(srv._MethodNotFound):
        handler._handle_method("unknown/method", {})


def test_handle_tools_call_bridge_not_initialized(srv):
    handler = _make_handler(srv)
    handler.__class__.bridge = None
    result = handler._handle_method(
        "tools/call", {"name": "get_app_info", "arguments": {}}
    )
    assert result.get("isError") is True
    assert "Bridge not initialized" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# _MCPHandler HTTP methods (do_OPTIONS / do_GET / do_POST / _process)
# ---------------------------------------------------------------------------


def test_do_options_sends_cors_and_200(srv):
    handler = _make_handler(srv)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.do_OPTIONS()
    handler.send_response.assert_called_once_with(200)
    handler.end_headers.assert_called_once()


def test_do_get_health_endpoint(srv):
    handler = _make_handler(srv)
    handler.path = "/health"
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.do_GET()
    handler.send_response.assert_called_once_with(200)
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body["status"] == "ok"


def test_do_get_root_endpoint(srv):
    handler = _make_handler(srv)
    handler.path = "/"
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.do_GET()
    handler.send_response.assert_called_once_with(200)


def test_do_get_unknown_path_404(srv):
    handler = _make_handler(srv)
    handler.path = "/nope"
    handler.send_error = MagicMock()
    handler.do_GET()
    handler.send_error.assert_called_once_with(404)


def test_do_post_wrong_path_404(srv):
    handler = _make_handler(srv)
    handler.path = "/other"
    handler.send_error = MagicMock()
    handler.do_POST()
    handler.send_error.assert_called_once_with(404, "Use POST /mcp")


def test_do_post_empty_body_400(srv):
    handler = _make_handler(srv)
    handler.path = "/mcp"
    handler.headers = {"Content-Length": "0"}
    handler.send_error = MagicMock()
    handler.do_POST()
    handler.send_error.assert_called_once_with(400, "Empty body")


def test_do_post_invalid_json_sends_parse_error(srv):
    handler = _make_handler(srv)
    handler.path = "/mcp"
    handler.headers = {"Content-Length": "7"}
    handler.rfile = MagicMock()
    handler.rfile.read.return_value = b"not json"
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.do_POST()
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body["error"]["code"] == -32700
    assert body["id"] is None


def test_process_notification_acknowledges_with_202(srv):
    handler = _make_handler(srv)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler._process({"method": "notifications/initialized"})
    handler.send_response.assert_called_once_with(202)
    handler.end_headers.assert_called_once()


def test_process_success_sends_result(srv):
    handler = _make_handler(srv)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler._process({"id": 1, "method": "ping", "params": {}})
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body == {"jsonrpc": "2.0", "result": {}, "id": 1}


def test_process_unknown_method_sends_method_not_found(srv):
    handler = _make_handler(srv)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler._process({"id": 7, "method": "unknown/method", "params": {}})
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body["error"]["code"] == -32601
    assert body["id"] == 7
    # Legacy era keeps HTTP 200 for JSON-RPC-level errors.
    handler.send_response.assert_called_once_with(200)


def test_process_method_exception_sends_internal_error(srv):
    handler = _make_handler(srv)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler._handle_method = MagicMock(side_effect=RuntimeError("boom"))
    handler._process({"id": 7, "method": "ping", "params": {}})
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body["error"]["code"] == -32603
    assert "boom" in body["error"]["message"]
    assert body["id"] == 7


def test_do_post_full_roundtrip(srv):
    """Exercises do_POST's happy path end-to-end (real _process call)."""
    handler = _make_handler(srv)
    handler.path = "/mcp"
    message = json.dumps({"id": 1, "method": "ping", "params": {}}).encode("utf-8")
    handler.headers = {"Content-Length": str(len(message))}
    handler.rfile = MagicMock()
    handler.rfile.read.return_value = message
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.do_POST()
    body = json.loads(handler.wfile.write.call_args[0][0])
    assert body == {"jsonrpc": "2.0", "result": {}, "id": 1}


def test_log_message_delegates_to_logger(srv):
    handler = _make_handler(srv)
    handler.log_message("%s bar", "foo")  # should not raise


# ---------------------------------------------------------------------------
# File I/O tools
# ---------------------------------------------------------------------------


def _file_bridge(srv_mod, tmp_path, extra_exts=None):
    """Bridge pre-configured with a tmp_path sandbox."""
    exts = extra_exts or [".txt", ".inp", ".xyz"]
    return make_bridge({
        "get_file_io_config": {
            "base_dir": str(tmp_path),
            "allowed_extensions": exts,
        },
        "set_file_io_config": {"success": True},
    })


def test_write_text_file_creates_file(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "write_text_file", {
        "path": "hello.txt", "content": "Hello, world!"
    })
    assert result.get("isError") is not True
    assert (tmp_path / "hello.txt").read_text() == "Hello, world!"


def test_write_text_file_creates_parents(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    srv.dispatch_tool(bridge, "write_text_file", {
        "path": "subdir/deep/mol.inp", "content": "! ORCA input"
    })
    assert (tmp_path / "subdir" / "deep" / "mol.inp").exists()


def test_write_text_file_no_overwrite_by_default(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    (tmp_path / "existing.txt").write_text("original")
    result = srv.dispatch_tool(bridge, "write_text_file", {
        "path": "existing.txt", "content": "new"
    })
    assert result.get("isError") is True
    assert (tmp_path / "existing.txt").read_text() == "original"


def test_write_text_file_overwrite_allowed(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    (tmp_path / "file.txt").write_text("old")
    srv.dispatch_tool(bridge, "write_text_file", {
        "path": "file.txt", "content": "new", "overwrite": True
    })
    assert (tmp_path / "file.txt").read_text() == "new"


def test_write_text_file_path_traversal_rejected(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "write_text_file", {
        "path": "../../evil.txt", "content": "bad"
    })
    assert result.get("isError") is True
    assert not (tmp_path.parent.parent / "evil.txt").exists()


def test_write_text_file_absolute_path_rejected(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "write_text_file", {
        "path": str(tmp_path / "abs.txt"), "content": "bad"
    })
    assert result.get("isError") is True


def test_write_text_file_extension_not_allowed(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path, extra_exts=[".txt"])
    result = srv.dispatch_tool(bridge, "write_text_file", {
        "path": "script.exe", "content": "bad"
    })
    assert result.get("isError") is True


def test_write_text_file_empty_path_rejected(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "write_text_file", {
        "path": "  ", "content": "bad"
    })
    assert result.get("isError") is True
    assert "path" in result["content"][0]["text"]


def test_write_text_file_no_extension_rejected(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "write_text_file", {
        "path": "README", "content": "hi"
    })
    assert result.get("isError") is True
    assert "no extension" in result["content"][0]["text"]


def test_write_text_file_content_too_large_rejected(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    huge = "x" * (5 * 1024 * 1024)
    result = srv.dispatch_tool(bridge, "write_text_file", {
        "path": "big.txt", "content": huge
    })
    assert result.get("isError") is True
    assert "MB limit" in result["content"][0]["text"]
    assert not (tmp_path / "big.txt").exists()


def test_write_file_with_xyz_block_empty_path_rejected(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "write_file_with_xyz_block", {
        "path": "", "content": "ignored"
    })
    assert result.get("isError") is True
    assert "path" in result["content"][0]["text"]


def test_read_text_file_ok(srv, tmp_path):
    (tmp_path / "data.txt").write_text("content here")
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "read_text_file", {"path": "data.txt"})
    assert result.get("isError") is not True
    assert "content here" in result["content"][0]["text"]


def test_read_text_file_not_found(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "read_text_file", {"path": "missing.txt"})
    assert result.get("isError") is True


def test_read_text_file_traversal_rejected(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "read_text_file", {"path": "../secret.txt"})
    assert result.get("isError") is True


def test_read_text_file_missing_path(srv):
    bridge = MagicMock()
    result = srv.dispatch_tool(bridge, "read_text_file", {})
    assert result.get("isError") is True
    bridge.call.assert_not_called()


def test_read_text_file_rejects_directory(srv, tmp_path):
    (tmp_path / "subdir.txt").mkdir()
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "read_text_file", {"path": "subdir.txt"})
    assert result.get("isError") is True
    assert "is not a file" in result["content"][0]["text"]


def test_read_text_file_too_large_rejected(srv, tmp_path, monkeypatch):
    (tmp_path / "big.txt").write_text("x" * 100)
    monkeypatch.setattr(srv, "_MAX_FILE_BYTES", 10)
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "read_text_file", {"path": "big.txt"})
    assert result.get("isError") is True
    assert "MB read limit" in result["content"][0]["text"]


def test_list_directory_ok(srv, tmp_path):
    (tmp_path / "mol.xyz").write_text("3\ntest\nC 0 0 0\n")
    (tmp_path / "sub").mkdir()
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "list_directory", {})
    text = result["content"][0]["text"]
    assert "mol.xyz" in text
    assert "sub" in text


def test_list_directory_traversal_rejected(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "list_directory", {"path": "../../"})
    assert result.get("isError") is True


def test_list_directory_nonexistent_rejected(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "list_directory", {"path": "nope"})
    assert result.get("isError") is True
    assert "does not exist" in result["content"][0]["text"]


def test_list_directory_not_a_directory_rejected(srv, tmp_path):
    (tmp_path / "file.txt").write_text("x")
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "list_directory", {"path": "file.txt"})
    assert result.get("isError") is True
    assert "is not a directory" in result["content"][0]["text"]


def test_list_directory_empty_reports_empty(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "list_directory", {})
    assert "(empty)" in result["content"][0]["text"]


def test_delete_file_requires_confirm(srv, tmp_path):
    (tmp_path / "bye.txt").write_text("delete me")
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "delete_file", {
        "path": "bye.txt", "confirm": False
    })
    assert result.get("isError") is True
    assert (tmp_path / "bye.txt").exists()


def test_delete_file_with_confirm(srv, tmp_path):
    (tmp_path / "gone.txt").write_text("bye")
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "delete_file", {
        "path": "gone.txt", "confirm": True
    })
    assert result.get("isError") is not True
    assert not (tmp_path / "gone.txt").exists()


def test_delete_file_traversal_rejected(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "delete_file", {
        "path": "../../important.txt", "confirm": True
    })
    assert result.get("isError") is True


def test_delete_file_missing_path(srv):
    bridge = MagicMock()
    result = srv.dispatch_tool(bridge, "delete_file", {"confirm": True})
    assert result.get("isError") is True
    bridge.call.assert_not_called()


def test_delete_file_not_exists_rejected(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "delete_file", {
        "path": "missing.txt", "confirm": True
    })
    assert result.get("isError") is True
    assert "does not exist" in result["content"][0]["text"]


def test_delete_file_rejects_directory(srv, tmp_path):
    (tmp_path / "dir.txt").mkdir()
    bridge = _file_bridge(srv, tmp_path)
    result = srv.dispatch_tool(bridge, "delete_file", {
        "path": "dir.txt", "confirm": True
    })
    assert result.get("isError") is True
    assert "not a regular file" in result["content"][0]["text"]


def test_get_file_io_config_no_base_dir(srv):
    bridge = make_bridge({
        "get_file_io_config": {"base_dir": None, "allowed_extensions": [".txt"]}
    })
    result = srv.dispatch_tool(bridge, "get_file_io_config", {})
    assert "not configured" in result["content"][0]["text"]


def test_set_file_io_config_valid(srv, tmp_path):
    bridge = make_bridge({"set_file_io_config": {"success": True}})
    result = srv.dispatch_tool(bridge, "set_file_io_config", {
        "base_dir": str(tmp_path), "allowed_extensions": [".inp", ".txt"]
    })
    assert result.get("isError") is not True
    bridge.call.assert_called_with("set_file_io_config", {
        "base_dir": str(tmp_path.resolve()),
        "allowed_extensions": [".inp", ".txt"],
    })


def test_set_file_io_config_nonexistent_dir(srv, tmp_path):
    bridge = make_bridge({"set_file_io_config": {"success": True}})
    result = srv.dispatch_tool(bridge, "set_file_io_config", {
        "base_dir": str(tmp_path / "does_not_exist"),
    })
    assert result.get("isError") is True


def test_set_file_io_config_no_args_rejected(srv):
    bridge = make_bridge({"set_file_io_config": {"success": True}})
    result = srv.dispatch_tool(bridge, "set_file_io_config", {})
    assert result.get("isError") is True
    assert "base_dir or allowed_extensions" in result["content"][0]["text"]


def test_file_io_no_base_dir_configured(srv, tmp_path):
    bridge = make_bridge({
        "get_file_io_config": {"base_dir": None, "allowed_extensions": [".txt"]}
    })
    result = srv.dispatch_tool(bridge, "write_text_file", {
        "path": "test.txt", "content": "x"
    })
    assert result.get("isError") is True
    assert "not configured" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# run_python
# ---------------------------------------------------------------------------


def test_run_python_stdout(srv):
    bridge = make_bridge({
        "run_python": {"stdout": "hello\n", "stderr": "", "result": "None"}
    })
    result = srv.dispatch_tool(bridge, "run_python", {"code": "print('hello')"})
    assert result.get("isError") is not True
    assert "hello" in result["content"][0]["text"]


def test_run_python_result_value(srv):
    bridge = make_bridge({
        "run_python": {"stdout": "", "stderr": "", "result": "42"}
    })
    result = srv.dispatch_tool(bridge, "run_python", {"code": "result = 42"})
    assert "42" in result["content"][0]["text"]


def test_run_python_no_output(srv):
    bridge = make_bridge({
        "run_python": {"stdout": "", "stderr": "", "result": "None"}
    })
    result = srv.dispatch_tool(bridge, "run_python", {"code": "pass"})
    assert "(no output)" in result["content"][0]["text"]


def test_run_python_stderr(srv):
    bridge = make_bridge({
        "run_python": {"stdout": "", "stderr": "warn!\n", "result": "None"}
    })
    result = srv.dispatch_tool(bridge, "run_python", {"code": "pass"})
    assert result.get("isError") is not True
    assert "warn!" in result["content"][0]["text"]


def test_run_python_empty_code(srv):
    bridge = make_bridge({})
    result = srv.dispatch_tool(bridge, "run_python", {"code": ""})
    assert result.get("isError") is True


# ---------------------------------------------------------------------------
# load_molecule_by_name
# ---------------------------------------------------------------------------


def test_load_molecule_by_name_ok(srv):
    bridge = make_bridge({"load_smiles": {"success": True}})
    with patch.object(srv, "_fetch_smiles_by_name", return_value="CC(=O)Oc1ccccc1C(=O)O"):
        result = srv.dispatch_tool(bridge, "load_molecule_by_name", {"name": "aspirin"})
    assert result.get("isError") is not True
    text = result["content"][0]["text"]
    assert "aspirin" in text
    assert "CC(=O)Oc1ccccc1C(=O)O" in text


def test_load_molecule_by_name_not_found(srv):
    bridge = make_bridge({})
    with patch.object(srv, "_fetch_smiles_by_name", side_effect=ValueError("not found")):
        result = srv.dispatch_tool(bridge, "load_molecule_by_name", {"name": "zzznonsense"})
    assert result.get("isError") is True


def _mock_urlopen_json(payload):
    """Return a context-manager mock mimicking urllib.request.urlopen."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def test_fetch_smiles_new_pubchem_key(srv):
    """PubChem's 2025 API returns 'SMILES' instead of 'IsomericSMILES'."""
    payload = {"PropertyTable": {"Properties": [
        {"CID": 2244, "SMILES": "CC(=O)OC1=CC=CC=C1C(=O)O"}
    ]}}
    with patch.object(srv.urllib.request, "urlopen", _mock_urlopen_json(payload)):
        assert srv._fetch_smiles_by_name("aspirin") == "CC(=O)OC1=CC=CC=C1C(=O)O"


def test_fetch_smiles_legacy_pubchem_key(srv):
    """Older responses with 'IsomericSMILES' are still accepted."""
    payload = {"PropertyTable": {"Properties": [
        {"CID": 2244, "IsomericSMILES": "CC(=O)OC1=CC=CC=C1C(=O)O"}
    ]}}
    with patch.object(srv.urllib.request, "urlopen", _mock_urlopen_json(payload)):
        assert srv._fetch_smiles_by_name("aspirin") == "CC(=O)OC1=CC=CC=C1C(=O)O"


def test_fetch_smiles_requests_smiles_property(srv):
    """The request URL must use the non-deprecated 'SMILES' property name."""
    payload = {"PropertyTable": {"Properties": [{"CID": 1, "SMILES": "C"}]}}
    urlopen = _mock_urlopen_json(payload)
    with patch.object(srv.urllib.request, "urlopen", urlopen):
        srv._fetch_smiles_by_name("methane")
    url = urlopen.call_args[0][0]
    assert "/property/SMILES/JSON" in url
    assert "IsomericSMILES" not in url


def test_fetch_smiles_no_smiles_key(srv):
    """A response missing any SMILES key raises a descriptive ValueError."""
    payload = {"PropertyTable": {"Properties": [{"CID": 2244}]}}
    with patch.object(srv.urllib.request, "urlopen", _mock_urlopen_json(payload)):
        with pytest.raises(ValueError, match="no SMILES"):
            srv._fetch_smiles_by_name("aspirin")


def test_fetch_smiles_404_not_found(srv):
    import urllib.error

    err = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
    with patch.object(srv.urllib.request, "urlopen", MagicMock(side_effect=err)):
        with pytest.raises(ValueError, match="not found on PubChem"):
            srv._fetch_smiles_by_name("zzznonsense")


def test_fetch_smiles_http_error(srv):
    import urllib.error

    err = urllib.error.HTTPError("url", 503, "Busy", {}, None)
    with patch.object(srv.urllib.request, "urlopen", MagicMock(side_effect=err)):
        with pytest.raises(ValueError, match="HTTP 503"):
            srv._fetch_smiles_by_name("aspirin")


def test_fetch_smiles_network_error(srv):
    with patch.object(srv.urllib.request, "urlopen", MagicMock(side_effect=OSError("boom"))):
        with pytest.raises(ValueError, match="PubChem lookup error"):
            srv._fetch_smiles_by_name("aspirin")


def test_load_molecule_by_name_empty(srv):
    bridge = make_bridge({})
    result = srv.dispatch_tool(bridge, "load_molecule_by_name", {"name": ""})
    assert result.get("isError") is True


# ---------------------------------------------------------------------------
# New molecule tools (push_undo_checkpoint, enter_3d_mode, etc.)
# ---------------------------------------------------------------------------


def test_push_undo_checkpoint(srv):
    bridge = make_bridge({"push_undo_checkpoint": {"success": True}})
    result = srv.dispatch_tool(bridge, "push_undo_checkpoint", {})
    assert result.get("isError") is not True
    assert "checkpoint" in result["content"][0]["text"].lower()


def test_get_mapped_smiles_tool_defined(srv):
    names = {t["name"] for t in srv._TOOLS}
    assert "get_mapped_smiles" in names


def test_get_mapped_smiles_dispatch(srv):
    bridge = make_bridge({
        "get_mapped_smiles": {
            "loaded": True,
            "mapped_smiles": "[CH3:1][OH:2]",
            "atoms": [
                {"index": 0, "map_num": 1, "symbol": "C"},
                {"index": 1, "map_num": 2, "symbol": "O"},
            ],
        }
    })
    result = srv.dispatch_tool(bridge, "get_mapped_smiles", {})
    assert result.get("isError") is not True
    text = result["content"][0]["text"]
    assert "[CH3:1][OH:2]" in text
    assert "atom_index 0: C (shown as :1)" in text
    assert "atom_index 1: O (shown as :2)" in text


def test_get_mapped_smiles_dispatch_no_molecule(srv):
    bridge = make_bridge({
        "get_mapped_smiles": {"loaded": False, "mapped_smiles": None, "atoms": []}
    })
    result = srv.dispatch_tool(bridge, "get_mapped_smiles", {})
    assert result.get("isError") is not True
    assert "No molecule" in result["content"][0]["text"]


def test_apply_reaction_smarts_tool_defined(srv):
    tool = next(t for t in srv._TOOLS if t["name"] == "apply_reaction_smarts")
    assert tool["inputSchema"]["required"] == ["reaction_smarts"]
    assert "atom_index" in tool["inputSchema"]["properties"]


def test_apply_reaction_smarts_dispatch(srv):
    bridge = make_bridge({
        "apply_reaction_smarts": {
            "success": True,
            "smiles": "Clc1ccccc1",
            "num_products": 6,
            "selected_product": 2,
            "converted_3d": True,
            "mapped_smiles": "[Cl:1][c:2]1[cH:3][cH:4][cH:5][cH:6][cH:7]1",
        }
    })
    result = srv.dispatch_tool(
        bridge,
        "apply_reaction_smarts",
        {"reaction_smarts": "[c:1][H]>>[c:1][Cl]", "atom_index": 2},
    )
    assert result.get("isError") is not True
    text = result["content"][0]["text"]
    assert "Clc1ccccc1" in text
    assert "[c:1][H]>>[c:1][Cl]" in text
    assert "match #2" in text
    assert "conversion triggered" in text
    assert "atom indices were reassigned" in text
    assert "[Cl:1][c:2]1[cH:3][cH:4][cH:5][cH:6][cH:7]1" in text


def test_apply_reaction_smarts_dispatch_error(srv):
    bridge = make_bridge({})
    bridge.call = MagicMock(side_effect=ValueError("pattern did not match"))
    result = srv.dispatch_tool(
        bridge, "apply_reaction_smarts", {"reaction_smarts": "[X:1]>>[Y:1]"}
    )
    assert result.get("isError") is True
    assert "did not match" in result["content"][0]["text"]


def test_exit_3d_mode_tool_defined(srv):
    names = {t["name"] for t in srv._TOOLS}
    assert "exit_3d_mode" in names


def test_exit_3d_mode_dispatch(srv):
    bridge = make_bridge({"exit_3d_mode": {"success": True}})
    result = srv.dispatch_tool(bridge, "exit_3d_mode", {})
    assert result.get("isError") is not True
    assert "2D editing" in result["content"][0]["text"]


def test_enter_3d_mode(srv):
    bridge = make_bridge({"enter_3d_mode": {"success": True}})
    result = srv.dispatch_tool(bridge, "enter_3d_mode", {})
    assert result.get("isError") is not True


def test_fit_2d_view(srv):
    bridge = make_bridge({"fit_2d_view": {"success": True}})
    result = srv.dispatch_tool(bridge, "fit_2d_view", {})
    assert result.get("isError") is not True


def test_reset_3d_camera(srv):
    bridge = make_bridge({"reset_3d_camera": {"success": True}})
    result = srv.dispatch_tool(bridge, "reset_3d_camera", {})
    assert result.get("isError") is not True


def test_refresh_3d_view(srv):
    bridge = make_bridge({"refresh_3d_view": {"success": True}})
    result = srv.dispatch_tool(bridge, "refresh_3d_view", {})
    assert result.get("isError") is not True


def test_check_chemistry(srv):
    bridge = make_bridge({"check_chemistry": {"success": True}})
    result = srv.dispatch_tool(bridge, "check_chemistry", {})
    assert result.get("isError") is not True


def test_refresh_ui(srv):
    bridge = make_bridge({"refresh_ui": {"success": True}})
    result = srv.dispatch_tool(bridge, "refresh_ui", {})
    assert result.get("isError") is not True


def test_highlight_bonds_ok(srv):
    bridge = make_bridge({"highlight_bonds": {"success": True, "bonds_colored": 2}})
    result = srv.dispatch_tool(bridge, "highlight_bonds", {
        "bond_colors": {"0": "#FF0000", "2": "#0000FF"}
    })
    assert result.get("isError") is not True
    assert "2 bond(s)" in result["content"][0]["text"]


def test_set_bond_color_override_atom_pairs(srv):
    bridge = make_bridge({"highlight_bonds": {"success": True, "bonds_colored": 1}})
    result = srv.dispatch_tool(bridge, "set_bond_color_override", {
        "atom_pair_colors": {"0-3": "#00FF00"}
    })
    assert result.get("isError") is not True
    assert "persists across redraws" in result["content"][0]["text"]
    args = bridge.call.call_args[0][1]
    assert args["atom_pair_colors"] == {"0-3": "#00FF00"}


def test_bond_override_tool_renamed(srv):
    names = [t["name"] for t in srv._TOOLS]
    assert "set_bond_color_override" in names
    assert "highlight_bonds" not in names


def test_highlight_bonds_empty(srv):
    bridge = make_bridge({})
    result = srv.dispatch_tool(bridge, "highlight_bonds", {"bond_colors": {}})
    assert result.get("isError") is True


# ---------------------------------------------------------------------------
# Plugin authoring helpers
# ---------------------------------------------------------------------------


def test_get_plugin_dev_manual_ok(srv):
    bridge = make_bridge({})
    with patch.object(srv, "_fetch_plugin_dev_manual", return_value="# Plugin Dev Manual\n..."):
        result = srv.dispatch_tool(bridge, "get_plugin_dev_manual", {})
    assert result.get("isError") is not True
    assert "Plugin Dev Manual" in result["content"][0]["text"]


def test_get_plugin_dev_manual_network_error(srv):
    bridge = make_bridge({})
    with patch.object(srv, "_fetch_plugin_dev_manual", side_effect=ValueError("network error")):
        result = srv.dispatch_tool(bridge, "get_plugin_dev_manual", {})
    assert result.get("isError") is True


def test_fetch_plugin_dev_manual_success(srv):
    """Exercises the real (unpatched) fetch body, not just dispatch_tool's
    error-handling wrapper."""
    resp = MagicMock()
    resp.read.return_value = b"# Real manual content"
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    with patch.object(srv.urllib.request, "urlopen", MagicMock(return_value=cm)):
        text = srv._fetch_plugin_dev_manual()
    assert "Real manual content" in text


def test_fetch_plugin_dev_manual_http_error(srv):
    import urllib.error

    err = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
    with patch.object(srv.urllib.request, "urlopen", MagicMock(side_effect=err)):
        with pytest.raises(ValueError, match="HTTP 404"):
            srv._fetch_plugin_dev_manual()


def test_fetch_plugin_dev_manual_generic_error(srv):
    with patch.object(srv.urllib.request, "urlopen", MagicMock(side_effect=OSError("offline"))):
        with pytest.raises(ValueError, match="Failed to fetch plugin development manual"):
            srv._fetch_plugin_dev_manual()


def test_get_app_source_ok(srv):
    bridge = make_bridge({"get_app_source": {"type": "file", "content": "# plugin_interface\n"}})
    result = srv.dispatch_tool(bridge, "get_app_source", {"path": "plugins/plugin_interface.py"})
    assert result.get("isError") is not True
    assert "plugin_interface" in result["content"][0]["text"]


def test_get_app_source_directory(srv):
    bridge = make_bridge({"get_app_source": {"type": "directory", "content": "Directory listing: .\n  [dir] plugins"}})
    result = srv.dispatch_tool(bridge, "get_app_source", {"path": "."})
    assert result.get("isError") is not True
    assert "plugins" in result["content"][0]["text"]


def test_get_app_source_empty_path(srv):
    bridge = make_bridge({})
    result = srv.dispatch_tool(bridge, "get_app_source", {"path": ""})
    assert result.get("isError") is True


def test_get_plugin_dir_ok(srv):
    bridge = make_bridge({"get_plugin_dir": {"plugin_dir": "/home/user/.moleditpy/plugins"}})
    result = srv.dispatch_tool(bridge, "get_plugin_dir", {})
    assert result.get("isError") is not True
    assert ".moleditpy/plugins" in result["content"][0]["text"]


def test_reload_plugins_ok(srv):
    bridge = make_bridge({"reload_plugins": {"success": True, "plugin_count": 3}})
    result = srv.dispatch_tool(bridge, "reload_plugins", {})
    assert result.get("isError") is not True
    assert "3" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# list_app_source_tree
# ---------------------------------------------------------------------------


def test_list_app_source_tree_ok(srv):
    bridge = make_bridge({"list_app_source_tree": {"content": "moleditpy/\n├── plugins/\n│   └── plugin_interface.py"}})
    result = srv.dispatch_tool(bridge, "list_app_source_tree", {})
    assert result.get("isError") is not True
    text = result["content"][0]["text"]
    assert "plugins" in text
    assert "plugin_interface.py" in text


def test_list_app_source_tree_subtree(srv):
    bridge = make_bridge({"list_app_source_tree": {"content": "plugins/\n└── plugin_interface.py"}})
    result = srv.dispatch_tool(bridge, "list_app_source_tree", {"path": "plugins"})
    assert result.get("isError") is not True


# ---------------------------------------------------------------------------
# write_file_with_xyz_block / format_xyz_block
# ---------------------------------------------------------------------------

_XYZ_ATOMS = [
    {"index": 0, "symbol": "C", "atomic_num": 6, "x": 0.0, "y": 0.0, "z": 0.0},
    {"index": 1, "symbol": "O", "atomic_num": 8, "x": 1.208, "y": 0.0, "z": 0.0},
    {"index": 2, "symbol": "H", "atomic_num": 1, "x": -0.55, "y": 0.92, "z": 0.0},
]


def _xyz_bridge(tmp_path, has_data=True):
    return make_bridge({
        "get_file_io_config": {
            "base_dir": str(tmp_path),
            "allowed_extensions": [".txt", ".inp", ".xyz", ".gjf"],
        },
        "get_xyz_atoms": {"atoms": _XYZ_ATOMS if has_data else [], "has_data": has_data},
    })


def test_format_xyz_block_default_symbol(srv):
    block = srv.format_xyz_block(_XYZ_ATOMS)
    lines = block.split("\n")
    assert len(lines) == 3
    assert lines[0].split() == ["C", "0.000000", "0.000000", "0.000000"]
    assert lines[1].split()[0] == "O"
    assert lines[1].split()[1] == "1.208000"


def test_format_xyz_block_atomic_number(srv):
    block = srv.format_xyz_block(_XYZ_ATOMS, element_style="atomic_number")
    firsts = [ln.split()[0] for ln in block.split("\n")]
    assert firsts == ["6", "8", "1"]


def test_format_xyz_block_symbol_and_number(srv):
    block = srv.format_xyz_block(_XYZ_ATOMS, element_style="symbol_and_number")
    parts = block.split("\n")[0].split()
    assert parts[0] == "C"
    assert parts[1] == "6.0"
    assert len(parts) == 5


def test_format_xyz_block_precision(srv):
    block = srv.format_xyz_block(_XYZ_ATOMS, precision=3)
    assert "1.208" in block
    assert "1.2080" not in block


def test_format_xyz_block_atom_order_reorder_and_subset(srv):
    block = srv.format_xyz_block(_XYZ_ATOMS, atom_order=[2, 0])
    firsts = [ln.split()[0] for ln in block.split("\n")]
    assert firsts == ["H", "C"]


def test_format_xyz_block_atom_order_duplicate_rejected(srv):
    with pytest.raises(ValueError, match="duplicate"):
        srv.format_xyz_block(_XYZ_ATOMS, atom_order=[0, 0])


def test_format_xyz_block_atom_order_invalid_index_rejected(srv):
    with pytest.raises(ValueError, match="invalid"):
        srv.format_xyz_block(_XYZ_ATOMS, atom_order=[0, 99])


def test_format_xyz_block_bad_style_rejected(srv):
    with pytest.raises(ValueError, match="element_style"):
        srv.format_xyz_block(_XYZ_ATOMS, element_style="nope")


def test_format_xyz_block_bad_precision_rejected(srv):
    with pytest.raises(ValueError, match="precision"):
        srv.format_xyz_block(_XYZ_ATOMS, precision=0)


def test_write_xyz_tool_is_registered(srv):
    names = [t["name"] for t in srv._TOOLS]
    assert "write_file_with_xyz_block" in names


def test_write_xyz_block_basic_file(srv, tmp_path):
    bridge = _xyz_bridge(tmp_path)
    result = srv.dispatch_tool(bridge, "write_file_with_xyz_block", {
        "path": "mol.inp",
        "header": "! B3LYP def2-SVP\n* xyz 0 1",
        "footer": "*",
    })
    assert result.get("isError") is not True
    text = (tmp_path / "mol.inp").read_text()
    lines = text.splitlines()
    assert lines[0] == "! B3LYP def2-SVP"
    assert lines[1] == "* xyz 0 1"
    assert lines[2].split()[0] == "C"
    assert lines[-1] == "*"
    assert "3 atom(s)" in result["content"][0]["text"]


def test_write_xyz_block_standard_xyz_header(srv, tmp_path):
    bridge = _xyz_bridge(tmp_path)
    srv.dispatch_tool(bridge, "write_file_with_xyz_block", {
        "path": "mol.xyz", "xyz_header": True, "comment": "formaldehyde",
    })
    lines = (tmp_path / "mol.xyz").read_text().splitlines()
    assert lines[0] == "3"
    assert lines[1] == "formaldehyde"
    assert lines[2].split()[0] == "C"


def test_write_xyz_block_atom_order_subset_count(srv, tmp_path):
    bridge = _xyz_bridge(tmp_path)
    result = srv.dispatch_tool(bridge, "write_file_with_xyz_block", {
        "path": "sub.xyz", "atom_order": [1], "xyz_header": True,
    })
    lines = (tmp_path / "sub.xyz").read_text().splitlines()
    assert lines[0] == "1"
    assert lines[2].split()[0] == "O"
    assert "1 atom(s)" in result["content"][0]["text"]


def test_write_xyz_block_no_3d_errors(srv, tmp_path):
    bridge = _xyz_bridge(tmp_path, has_data=False)
    result = srv.dispatch_tool(bridge, "write_file_with_xyz_block", {"path": "mol.xyz"})
    assert result.get("isError") is True
    assert "No 3D coordinates" in result["content"][0]["text"]
    assert not (tmp_path / "mol.xyz").exists()


def test_write_xyz_block_no_overwrite_by_default(srv, tmp_path):
    bridge = _xyz_bridge(tmp_path)
    (tmp_path / "mol.xyz").write_text("original")
    result = srv.dispatch_tool(bridge, "write_file_with_xyz_block", {"path": "mol.xyz"})
    assert result.get("isError") is True
    assert (tmp_path / "mol.xyz").read_text() == "original"


def test_write_xyz_block_extension_checked(srv, tmp_path):
    bridge = _xyz_bridge(tmp_path)
    result = srv.dispatch_tool(bridge, "write_file_with_xyz_block", {"path": "mol.exe"})
    assert result.get("isError") is True


def test_write_xyz_block_bad_atom_order_is_tool_error(srv, tmp_path):
    bridge = _xyz_bridge(tmp_path)
    result = srv.dispatch_tool(bridge, "write_file_with_xyz_block", {
        "path": "mol.xyz", "atom_order": [0, 0],
    })
    assert result.get("isError") is True


def test_write_xyz_block_content_too_large_rejected(srv, tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "_MAX_FILE_BYTES", 10)
    bridge = _xyz_bridge(tmp_path)
    result = srv.dispatch_tool(bridge, "write_file_with_xyz_block", {"path": "mol.xyz"})
    assert result.get("isError") is True
    assert "MB limit" in result["content"][0]["text"]
    assert not (tmp_path / "mol.xyz").exists()


# ---------------------------------------------------------------------------
# list_available_plugins / open_plugin_installer
# ---------------------------------------------------------------------------

_REGISTRY_JSON = json.dumps([
    {"name": "Cool Analyzer", "version": "1.0.0", "visible": True,
     "description": "Analyzes things.", "tags": ["Analysis"]},
    {"name": "Hidden Legacy", "version": "0.1", "visible": False,
     "description": "Old.", "tags": []},
    {"name": "ORCA Input Generator Neo", "version": "2026.01.01", "visible": True,
     "description": "Generates ORCA inputs.", "tags": ["DFT", "Generator"]},
]).encode("utf-8")


def _mock_urlopen(payload):
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=payload)))
    cm.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=cm)


def test_list_available_plugins_hides_invisible(srv):
    bridge = make_bridge({})
    with patch.object(srv.urllib.request, "urlopen", _mock_urlopen(_REGISTRY_JSON)):
        result = srv.dispatch_tool(bridge, "list_available_plugins", {})
    text = result["content"][0]["text"]
    assert result.get("isError") is not True
    assert "Cool Analyzer" in text
    assert "ORCA Input Generator Neo" in text
    assert "Hidden Legacy" not in text
    assert "2 plugin(s)" in text


def test_list_available_plugins_search_filter(srv):
    bridge = make_bridge({})
    with patch.object(srv.urllib.request, "urlopen", _mock_urlopen(_REGISTRY_JSON)):
        result = srv.dispatch_tool(bridge, "list_available_plugins", {"search": "orca"})
    text = result["content"][0]["text"]
    assert "ORCA Input Generator Neo" in text
    assert "Cool Analyzer" not in text


def test_list_available_plugins_no_match(srv):
    bridge = make_bridge({})
    with patch.object(srv.urllib.request, "urlopen", _mock_urlopen(_REGISTRY_JSON)):
        result = srv.dispatch_tool(bridge, "list_available_plugins", {"search": "zzz"})
    assert "No plugins" in result["content"][0]["text"]


def test_list_available_plugins_network_error(srv):
    bridge = make_bridge({})
    with patch.object(srv.urllib.request, "urlopen", MagicMock(side_effect=OSError("offline"))):
        result = srv.dispatch_tool(bridge, "list_available_plugins", {})
    assert result.get("isError") is True
    assert "Could not fetch" in result["content"][0]["text"]


def test_open_plugin_installer_found(srv):
    bridge = make_bridge({"open_plugin_installer": {"found": True}})
    result = srv.dispatch_tool(bridge, "open_plugin_installer", {})
    assert result.get("isError") is not True
    assert "opened" in result["content"][0]["text"]


def test_open_plugin_installer_missing(srv):
    bridge = make_bridge({"open_plugin_installer": {"found": False}})
    result = srv.dispatch_tool(bridge, "open_plugin_installer", {})
    assert result.get("isError") is True
    assert "not installed" in result["content"][0]["text"]


def test_new_plugin_tools_registered(srv):
    names = [t["name"] for t in srv._TOOLS]
    assert "list_available_plugins" in names
    assert "open_plugin_installer" in names


def test_write_xyz_block_header_footer_as_line_arrays(srv, tmp_path):
    bridge = _xyz_bridge(tmp_path)
    result = srv.dispatch_tool(bridge, "write_file_with_xyz_block", {
        "path": "arr.inp",
        "header": ["! B3LYP def2-SVP", "* xyz 0 1"],
        "footer": ["*", "# end"],
    })
    assert result.get("isError") is not True
    lines = (tmp_path / "arr.inp").read_text().splitlines()
    assert lines[0] == "! B3LYP def2-SVP"
    assert lines[1] == "* xyz 0 1"
    assert lines[2].split()[0] == "C"
    assert lines[-2] == "*"
    assert lines[-1] == "# end"


def test_write_xyz_block_string_header_still_works(srv, tmp_path):
    bridge = _xyz_bridge(tmp_path)
    srv.dispatch_tool(bridge, "write_file_with_xyz_block", {
        "path": "str.inp", "header": "%mem 4GB\n! Opt",
    })
    lines = (tmp_path / "str.inp").read_text().splitlines()
    assert lines[0] == "%mem 4GB"
    assert lines[1] == "! Opt"


def test_write_text_file_content_as_line_array(srv, tmp_path):
    bridge = _file_bridge(srv, tmp_path)
    srv.dispatch_tool(bridge, "write_text_file", {
        "path": "arr.txt", "content": ["line1", "line2"],
    })
    assert (tmp_path / "arr.txt").read_text() == "line1\nline2"


def test_run_python_code_as_line_array(srv):
    bridge = make_bridge({"run_python": {"stdout": "", "stderr": "", "result": "4"}})
    result = srv.dispatch_tool(bridge, "run_python", {"code": ["x = 2 + 2", "result = x"]})
    assert result.get("isError") is not True
    op, args = bridge.call.call_args[0][0], bridge.call.call_args[0][1]
    assert op == "run_python"
    assert args["code"] == "x = 2 + 2\nresult = x"


def test_show_xyz_text_as_line_array(srv):
    bridge = make_bridge({"show_xyz": {"success": True}})
    srv.dispatch_tool(bridge, "show_xyz_in_viewer", {"xyz_text": ["C 0 0 0", "O 1 0 0"]})
    args = bridge.call.call_args[0][1]
    assert args["xyz_text"] == "C 0 0 0\nO 1 0 0"


def test_load_mol_block_as_line_array(srv):
    bridge = make_bridge({"load_mol_block": {"success": True, "num_atoms": 1}})
    srv.dispatch_tool(bridge, "load_from_mol_block", {"mol_block": ["line1", "M  END"]})
    args = bridge.call.call_args[0][1]
    assert args["mol_block"] == "line1\nM  END"


# ---------------------------------------------------------------------------
# set_cpk_color_override / reset_cpk_color_override
# ---------------------------------------------------------------------------


def test_set_cpk_color_override_dispatch(srv):
    bridge = make_bridge({"highlight_atoms": {"success": True}})
    result = srv.dispatch_tool(bridge, "set_cpk_color_override", {
        "atom_colors": {"0": "#FF0000"},
    })
    assert result.get("isError") is not True
    assert "persists across redraws" in result["content"][0]["text"]


def test_set_cpk_color_override_missing_arg(srv):
    bridge = MagicMock()
    result = srv.dispatch_tool(bridge, "set_cpk_color_override", {})
    assert result.get("isError") is True
    bridge.call.assert_not_called()


def test_highlight_atoms_legacy_alias_still_dispatches(srv):
    bridge = make_bridge({"highlight_atoms": {"success": True}})
    result = srv.dispatch_tool(bridge, "highlight_atoms", {
        "atom_colors": {"0": "#FF0000"},
    })
    assert result.get("isError") is not True


def test_reset_cpk_color_override_dispatch(srv):
    bridge = make_bridge({"reset_cpk_color_override": {"cleared_atoms": 3, "cleared_bonds": 0}})
    result = srv.dispatch_tool(bridge, "reset_cpk_color_override", {"scope": "atoms"})
    assert result.get("isError") is not True
    assert "3 atom(s)" in result["content"][0]["text"]
    assert bridge.call.call_args[0][1] == {"scope": "atoms"}


def test_cpk_tools_registered_highlight_atoms_renamed(srv):
    names = [t["name"] for t in srv._TOOLS]
    assert "set_cpk_color_override" in names
    assert "reset_cpk_color_override" in names
    assert "highlight_atoms" not in names

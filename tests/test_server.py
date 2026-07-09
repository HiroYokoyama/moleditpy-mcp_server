"""Tests for mcp_server/server.py — MCP protocol and tool dispatch."""

from __future__ import annotations

import json
from typing import Any, Dict
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
    bridge = make_bridge({"highlight_bonds": {"success": True}})
    result = srv.dispatch_tool(bridge, "highlight_bonds", {
        "bond_colors": {"0": "#FF0000", "2": "#0000FF"}
    })
    assert result.get("isError") is not True
    assert "2" in result["content"][0]["text"]


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

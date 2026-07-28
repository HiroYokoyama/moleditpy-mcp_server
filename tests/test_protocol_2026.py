"""Tests for the 2026-07-28 (stateless) MCP protocol support in server.py."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from conftest import load_module, mock_optional_imports


@pytest.fixture()
def srv():
    with mock_optional_imports():
        yield load_module("server.py")


MODERN = "2026-07-28"


def _make_handler(srv_mod: Any, mode: str = "auto") -> Any:
    cls = srv_mod._MCPHandler
    cls.bridge = MagicMock()
    cls.session_id = "test-session"
    cls.server_name = "Test Server"
    cls.server_version = "1.0"
    cls.protocol_mode = mode
    handler = object.__new__(cls)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    return handler


def _modern_message(method: str = "tools/list", **params: Any) -> dict:
    body = dict(params)
    body["_meta"] = {"io.modelcontextprotocol/protocolVersion": MODERN}
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": body}


def _modern_headers(method: str = "tools/list", name: str | None = None) -> dict:
    headers = {"mcp-protocol-version": MODERN, "mcp-method": method}
    if name is not None:
        headers["mcp-name"] = name
    return headers


def _sent_body(handler: Any) -> dict:
    return json.loads(handler.wfile.write.call_args[0][0])


def _sent_headers(handler: Any) -> dict:
    return {
        call.args[0].lower(): call.args[1]
        for call in handler.send_header.call_args_list
    }


# ---------------------------------------------------------------------------
# supported_versions / negotiate_legacy_version
# ---------------------------------------------------------------------------


def test_supported_versions_auto_lists_both_eras(srv):
    versions = srv.supported_versions("auto")
    assert versions[0] == MODERN
    assert "2024-11-05" in versions and "2025-11-25" in versions


def test_supported_versions_modern_only(srv):
    assert srv.supported_versions("modern") == [MODERN]


def test_supported_versions_legacy_only(srv):
    versions = srv.supported_versions("legacy")
    assert MODERN not in versions
    assert "2024-11-05" in versions


def test_supported_versions_defaults_to_auto(srv):
    assert srv.supported_versions() == srv.supported_versions("auto")


def test_negotiate_legacy_version_echoes_known_version(srv):
    assert srv.negotiate_legacy_version("2025-06-18") == "2025-06-18"


@pytest.mark.parametrize("requested", [None, "1999-01-01", 42, MODERN])
def test_negotiate_legacy_version_falls_back(srv, requested):
    assert srv.negotiate_legacy_version(requested) == srv._PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Header value encoding
# ---------------------------------------------------------------------------


def test_decode_header_value_passes_plain_ascii_through(srv):
    assert srv.decode_header_value("get_weather") == "get_weather"


def test_decode_header_value_decodes_sentinel(srv):
    encoded = base64.b64encode("Hello, 世界".encode()).decode()
    assert srv.decode_header_value(f"=?base64?{encoded}?=") == "Hello, 世界"


def test_decode_header_value_returns_input_on_bad_base64(srv):
    assert srv.decode_header_value("=?base64?!!!not-base64!!!?=") == (
        "=?base64?!!!not-base64!!!?="
    )


# ---------------------------------------------------------------------------
# Era detection
# ---------------------------------------------------------------------------


def test_is_modern_request_true_for_discover(srv):
    assert srv.is_modern_request({"method": "server/discover"}, {}) is True


def test_is_modern_request_true_from_body_meta(srv):
    assert srv.is_modern_request(_modern_message(), {}) is True


def test_is_modern_request_true_from_header_only(srv):
    message = {"method": "tools/list", "params": {}}
    assert srv.is_modern_request(message, {"mcp-protocol-version": MODERN}) is True


def test_is_modern_request_false_for_legacy_initialize(srv):
    message = {"method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    assert srv.is_modern_request(message, {"mcp-protocol-version": "2025-06-18"}) is False


def test_is_modern_request_tolerates_non_dict_meta(srv):
    message = {"method": "tools/list", "params": {"_meta": "nonsense"}}
    assert srv.is_modern_request(message, {}) is False


# ---------------------------------------------------------------------------
# Modern request validation
# ---------------------------------------------------------------------------


def test_validate_modern_request_accepts_well_formed_request(srv):
    assert srv.validate_modern_request(_modern_message(), _modern_headers()) is None


def test_validate_modern_request_accepts_tools_call_with_name_header(srv):
    message = _modern_message("tools/call", name="get_app_info", arguments={})
    headers = _modern_headers("tools/call", name="get_app_info")
    assert srv.validate_modern_request(message, headers) is None


def test_validate_modern_request_decodes_base64_name_header(srv):
    encoded = base64.b64encode(b"get_app_info").decode()
    message = _modern_message("tools/call", name="get_app_info", arguments={})
    headers = _modern_headers("tools/call", name=f"=?base64?{encoded}?=")
    assert srv.validate_modern_request(message, headers) is None


def test_validate_modern_request_rejects_missing_version_header(srv):
    error = srv.validate_modern_request(_modern_message(), {"mcp-method": "tools/list"})
    assert error["code"] == -32020
    assert "MCP-Protocol-Version" in error["message"]


def test_validate_modern_request_rejects_header_body_version_mismatch(srv):
    headers = _modern_headers()
    headers["mcp-protocol-version"] = "2025-11-25"
    error = srv.validate_modern_request(_modern_message(), headers)
    assert error["code"] == -32020
    assert "does not match body" in error["message"]


def test_validate_modern_request_rejects_unsupported_version(srv):
    message = {
        "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1999-01-01"}},
    }
    headers = {"mcp-protocol-version": "1999-01-01", "mcp-method": "tools/list"}
    error = srv.validate_modern_request(message, headers)
    assert error["code"] == -32022
    assert error["data"]["requested"] == "1999-01-01"
    assert MODERN in error["data"]["supported"]


def test_validate_modern_request_unsupported_version_lists_mode_versions(srv):
    message = {
        "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2024-11-05"}},
    }
    headers = {"mcp-protocol-version": "2024-11-05", "mcp-method": "tools/list"}
    error = srv.validate_modern_request(message, headers, mode="modern")
    assert error["code"] == -32022
    assert error["data"]["supported"] == [MODERN]


def test_validate_modern_request_rejects_missing_method_header(srv):
    error = srv.validate_modern_request(
        _modern_message(), {"mcp-protocol-version": MODERN}
    )
    assert error["code"] == -32020
    assert "Mcp-Method" in error["message"]


def test_validate_modern_request_rejects_method_header_mismatch(srv):
    error = srv.validate_modern_request(
        _modern_message(), _modern_headers("tools/call")
    )
    assert error["code"] == -32020
    assert "tools/call" in error["message"]


def test_validate_modern_request_rejects_missing_name_header(srv):
    message = _modern_message("tools/call", name="get_app_info", arguments={})
    error = srv.validate_modern_request(message, _modern_headers("tools/call"))
    assert error["code"] == -32020
    assert "Mcp-Name" in error["message"]


def test_validate_modern_request_rejects_name_header_mismatch(srv):
    message = _modern_message("tools/call", name="get_app_info", arguments={})
    headers = _modern_headers("tools/call", name="clear_canvas")
    error = srv.validate_modern_request(message, headers)
    assert error["code"] == -32020
    assert "params.name" in error["message"]


def test_validate_modern_request_allows_header_only_version(srv):
    """A client that omits _meta but sends the header is still valid."""
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert srv.validate_modern_request(message, _modern_headers()) is None


# ---------------------------------------------------------------------------
# server/discover
# ---------------------------------------------------------------------------


def test_build_discover_result_shape(srv):
    result = srv.build_discover_result("auto", "Srv", "9.9")
    assert result["supportedVersions"][0] == MODERN
    assert result["capabilities"] == {"tools": {}}
    assert result["cacheScope"] == "private"
    assert result["ttlMs"] > 0
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == {
        "name": "Srv",
        "version": "9.9",
    }


def test_discover_instructions_guide_the_model(srv):
    instructions = srv.build_discover_result("auto", "Srv", "1")["instructions"]
    assert "MoleditPy" in instructions
    assert "push_undo_checkpoint" in instructions
    assert "grep_files" in instructions


def test_handle_discover_via_handler(srv):
    handler = _make_handler(srv)
    result = handler._handle_method("server/discover", {})
    assert result["supportedVersions"] == srv.supported_versions("auto")


def test_bare_discover_probe_is_accepted(srv):
    """A client probing for supported versions has none to declare yet."""
    assert srv.validate_modern_request(
        {"method": "server/discover", "params": {}}, {}
    ) is None


def test_discover_with_headers_is_still_validated(srv):
    error = srv.validate_modern_request(
        {"method": "server/discover", "params": {}},
        {"mcp-protocol-version": MODERN},  # Mcp-Method missing
    )
    assert error["code"] == -32020


def test_discover_roundtrip_sends_200_without_session_header(srv):
    handler = _make_handler(srv)
    handler._process(
        {"jsonrpc": "2.0", "id": 3, "method": "server/discover", "params": {}}
    )
    handler.send_response.assert_called_once_with(200)
    assert "mcp-session-id" not in _sent_headers(handler)
    body = _sent_body(handler)
    assert body["result"]["supportedVersions"][0] == MODERN


# ---------------------------------------------------------------------------
# tools/list cache hints and result _meta
# ---------------------------------------------------------------------------


def test_tools_list_modern_carries_cache_hints(srv):
    handler = _make_handler(srv)
    result = handler._handle_method("tools/list", {}, modern=True)
    assert result["ttlMs"] == srv._TOOLS_TTL_MS
    assert result["cacheScope"] == "private"


def test_tools_list_legacy_has_no_cache_hints(srv):
    handler = _make_handler(srv)
    result = handler._handle_method("tools/list", {})
    assert "ttlMs" not in result and "cacheScope" not in result


def test_modern_result_gets_server_info_meta(srv):
    handler = _make_handler(srv)
    handler.headers = _modern_headers()
    handler._process(_modern_message())
    body = _sent_body(handler)
    assert body["result"]["_meta"]["io.modelcontextprotocol/serverInfo"] == {
        "name": "Test Server",
        "version": "1.0",
    }


def test_legacy_result_keeps_session_header(srv):
    handler = _make_handler(srv)
    handler._process({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    assert _sent_headers(handler)["mcp-session-id"] == "test-session"


# ---------------------------------------------------------------------------
# Legacy initialize
# ---------------------------------------------------------------------------


def test_initialize_echoes_requested_legacy_version(srv):
    handler = _make_handler(srv)
    result = handler._handle_method("initialize", {"protocolVersion": "2025-06-18"})
    assert result["protocolVersion"] == "2025-06-18"
    assert "MoleditPy" in result["instructions"]


def test_initialize_falls_back_for_unknown_version(srv):
    handler = _make_handler(srv)
    result = handler._handle_method("initialize", {"protocolVersion": "1999-01-01"})
    assert result["protocolVersion"] == srv._PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


def test_modern_request_rejected_in_legacy_mode(srv):
    handler = _make_handler(srv, mode="legacy")
    handler.headers = _modern_headers()
    handler._process(_modern_message())
    handler.send_response.assert_called_once_with(400)
    error = _sent_body(handler)["error"]
    assert error["code"] == -32022
    assert MODERN not in error["data"]["supported"]
    assert error["data"]["requested"] == MODERN


def test_legacy_initialize_rejected_in_modern_mode(srv):
    handler = _make_handler(srv, mode="modern")
    handler._process(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    handler.send_response.assert_called_once_with(404)
    error = _sent_body(handler)["error"]
    assert error["code"] == -32601
    assert MODERN in error["message"]


def test_modern_request_served_in_modern_mode(srv):
    handler = _make_handler(srv, mode="modern")
    handler.headers = _modern_headers()
    handler._process(_modern_message())
    handler.send_response.assert_called_once_with(200)
    assert "tools" in _sent_body(handler)["result"]


def test_legacy_request_served_in_auto_mode(srv):
    handler = _make_handler(srv, mode="auto")
    handler._process(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    handler.send_response.assert_called_once_with(200)
    assert _sent_body(handler)["result"]["protocolVersion"] == srv._PROTOCOL_VERSION


def test_header_validation_failure_returns_400(srv):
    handler = _make_handler(srv)
    handler.headers = {"MCP-Protocol-Version": MODERN}  # no Mcp-Method
    handler._process(_modern_message())
    handler.send_response.assert_called_once_with(400)
    assert _sent_body(handler)["error"]["code"] == -32020


def test_header_names_are_matched_case_insensitively(srv):
    handler = _make_handler(srv)
    handler.headers = {
        "MCP-Protocol-Version": MODERN,
        "MCP-METHOD": "tools/list",
    }
    handler._process(_modern_message())
    handler.send_response.assert_called_once_with(200)


def test_unknown_modern_method_returns_404(srv):
    handler = _make_handler(srv)
    handler.headers = _modern_headers("does/not-exist")
    handler._process(_modern_message("does/not-exist"))
    handler.send_response.assert_called_once_with(404)
    assert _sent_body(handler)["error"]["code"] == -32601


def test_modern_notification_still_acknowledged(srv):
    handler = _make_handler(srv)
    handler.headers = _modern_headers("notifications/progress")
    handler._process({"jsonrpc": "2.0", "method": "notifications/progress"})
    handler.send_response.assert_called_once_with(202)


# ---------------------------------------------------------------------------
# Transport-level behaviour
# ---------------------------------------------------------------------------


def test_get_mcp_endpoint_is_405(srv):
    handler = _make_handler(srv)
    handler.path = "/mcp"
    handler.send_error = MagicMock()
    handler.do_GET()
    assert handler.send_error.call_args[0][0] == 405


def test_delete_mcp_endpoint_is_405(srv):
    handler = _make_handler(srv)
    handler.path = "/mcp"
    handler.send_error = MagicMock()
    handler.do_DELETE()
    assert handler.send_error.call_args[0][0] == 405


def test_delete_other_path_is_404(srv):
    handler = _make_handler(srv)
    handler.path = "/nope"
    handler.send_error = MagicMock()
    handler.do_DELETE()
    assert handler.send_error.call_args[0][0] == 404


def test_health_reports_protocol_mode_and_versions(srv):
    handler = _make_handler(srv, mode="modern")
    handler.path = "/health"
    handler.do_GET()
    body = _sent_body(handler)
    assert body["protocolMode"] == "modern"
    assert body["supportedVersions"] == [MODERN]
    assert body["version"] == "1.0"


def test_cors_allows_the_mirrored_headers(srv):
    handler = _make_handler(srv)
    handler._send_cors()
    allowed = _sent_headers(handler)["access-control-allow-headers"]
    for header in ("MCP-Protocol-Version", "Mcp-Method", "Mcp-Name"):
        assert header in allowed


# ---------------------------------------------------------------------------
# MCPHttpServer wiring
# ---------------------------------------------------------------------------


def test_http_server_defaults_to_auto_mode(srv):
    server = srv.MCPHttpServer(MagicMock(), "n", "v")
    assert server.protocol_mode == "auto"


@pytest.mark.parametrize("mode", ["auto", "legacy", "modern"])
def test_http_server_accepts_valid_modes(srv, mode):
    assert srv.MCPHttpServer(MagicMock(), "n", "v", protocol_mode=mode).protocol_mode == mode


def test_http_server_rejects_unknown_mode(srv):
    server = srv.MCPHttpServer(MagicMock(), "n", "v", protocol_mode="bogus")
    assert server.protocol_mode == "auto"


def test_start_stores_config_on_the_server_instance(srv):
    """Per-instance config keeps two servers in one process independent."""
    first = srv.MCPHttpServer(MagicMock(), "first", "1", port=0, protocol_mode="modern")
    second = srv.MCPHttpServer(MagicMock(), "second", "2", port=0, protocol_mode="legacy")
    first.start()
    second.start()
    try:
        assert first._httpd.mcp_protocol_mode == "modern"
        assert second._httpd.mcp_protocol_mode == "legacy"
        assert first._httpd.mcp_server_name == "first"
        assert second._httpd.mcp_server_name == "second"
        assert first._httpd.mcp_session_id != second._httpd.mcp_session_id
    finally:
        first.stop()
        second.stop()


def test_handler_reads_config_from_its_server(srv):
    handler = _make_handler(srv, mode="auto")
    handler.server = MagicMock(
        mcp_protocol_mode="modern", mcp_server_name="From Server"
    )
    assert handler._cfg("protocol_mode") == "modern"
    assert handler._cfg("server_name") == "From Server"


def test_handler_falls_back_to_class_defaults(srv):
    handler = _make_handler(srv, mode="legacy")
    assert handler._cfg("protocol_mode") == "legacy"


def test_protocol_modes_constant(srv):
    assert srv.PROTOCOL_MODES == ("auto", "legacy", "modern")

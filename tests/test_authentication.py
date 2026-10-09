"""Exercise authentication on a real isolated HTTP server."""

import http.client
import json
from unittest.mock import MagicMock

import pytest
from conftest import load_module, mock_optional_imports


@pytest.mark.parametrize("token", [None, "wrong", "audit-token"])
def test_python_dispatch_requires_bearer_token(token):
    with mock_optional_imports():
        srv = load_module("server.py")
    bridge = MagicMock()
    bridge.call.return_value = {"result": "audit-marker"}
    server = srv.MCPHttpServer(bridge, "Audit", "0", port=0)
    server.auth_token = "audit-token"
    server.start()
    try:
        port = server._httpd.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "run_python",
                    "arguments": {"code": "result = 'audit-marker'"},
                },
            }
        )
        conn.request("POST", "/mcp", body=body, headers=headers)
        response = conn.getresponse()
        response.read()
        assert response.status == (200 if token == "audit-token" else 401)
        if token == "audit-token":
            bridge.call.assert_called_once_with(
                "run_python", {"code": "result = 'audit-marker'"}, timeout=30.0
            )
        else:
            bridge.call.assert_not_called()
        conn.close()
    finally:
        server.stop()


def test_empty_token_configuration_fails_closed():
    with mock_optional_imports():
        srv = load_module("server.py")
    handler = object.__new__(srv._MCPHandler)
    handler.headers = {"Authorization": "Bearer "}
    assert not handler._authenticated()

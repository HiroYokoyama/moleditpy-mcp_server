"""
Branch coverage for mcp_gui_tester's transport and era negotiation, driven
with a stubbed urlopen instead of a live server (the paths here are error
and fallback cases a real server does not produce on demand).
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from test_mcp_tester import _has_pyqt6, _load_tester

pytestmark = pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")

URL = "http://127.0.0.1:1/mcp"


@pytest.fixture()
def mod():
    return _load_tester()


class _Response:
    def __init__(self, body: str, headers=None, status: int = 200) -> None:
        self._body = body.encode("utf-8")
        self.headers = headers or {}
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_urlopen(mod, responses):
    """Feed *responses* (callables or objects) to successive requests."""
    sent = []

    def _fake(req, *args, **kwargs):
        sent.append(json.loads(req.data.decode("utf-8")))
        item = responses[min(len(sent) - 1, len(responses) - 1)]
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return item

    mod.urllib.request.urlopen = _fake
    return sent


@pytest.fixture(autouse=True)
def _restore_urlopen(mod):
    real = mod.urllib.request.urlopen
    yield
    mod.urllib.request.urlopen = real


def _http_error(mod, code: int, payload) -> urllib.error.HTTPError:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return urllib.error.HTTPError(
        URL, code, "Bad Request", {}, io.BytesIO(body.encode("utf-8"))
    )


def test_empty_response_body_is_treated_as_no_result(mod):
    client = mod.MCPClient(URL, protocol="legacy")
    _stub_urlopen(mod, [_Response("   ")])
    assert client._rpc("notifications/ping") is None


def test_error_body_returned_with_error_status_raises(mod):
    client = mod.MCPClient(URL, protocol="modern")
    _stub_urlopen(
        mod,
        [
            _http_error(
                mod,
                400,
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32020, "message": "bad header"}},
            )
        ],
    )
    with pytest.raises(mod.MCPError) as excinfo:
        client.list_tools()
    assert excinfo.value.code == -32020
    assert excinfo.value.http_status == 400


def test_retry_gives_up_when_no_newer_version_offered(mod):
    client = mod.MCPClient(URL, protocol="modern")
    _stub_urlopen(
        mod,
        [
            _http_error(
                mod,
                400,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {
                        "code": -32022,
                        "message": "Unsupported protocol version",
                        "data": {"supported": ["2025-06-18"], "requested": "2026-07-28"},
                    },
                },
            )
        ],
    )
    with pytest.raises(mod.MCPError):
        client.list_tools()
    assert client.supported_versions == ["2025-06-18"]


def test_auto_falls_back_when_server_offers_only_legacy_versions(mod):
    client = mod.MCPClient(URL, protocol="auto")
    unsupported = _http_error(
        mod,
        400,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32022,
                "message": "Unsupported protocol version",
                "data": {"supported": ["2024-11-05"], "requested": "2026-07-28"},
            },
        },
    )
    handshake = _Response(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "Old", "version": "1"},
                    "capabilities": {"tools": {}},
                },
            }
        ),
        headers={"Mcp-Session-Id": "abc"},
    )
    _stub_urlopen(mod, [unsupported, handshake])
    info = client.connect()
    assert info["era"] == "legacy"
    assert client.session_id == "abc"


def test_auto_does_not_fall_back_on_other_modern_errors(mod):
    client = mod.MCPClient(URL, protocol="auto")
    _stub_urlopen(
        mod,
        [
            _http_error(
                mod,
                400,
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -32020, "message": "bad header"}},
            )
        ],
    )
    with pytest.raises(mod.MCPError) as excinfo:
        client.connect()
    assert excinfo.value.code == -32020


def test_auto_falls_back_when_discover_is_not_json(mod):
    client = mod.MCPClient(URL, protocol="auto")
    handshake = _Response(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"protocolVersion": "2024-11-05", "serverInfo": {}},
            }
        )
    )
    _stub_urlopen(mod, [_http_error(mod, 404, "<html>not found</html>"), handshake])
    assert client.connect()["era"] == "legacy"


def test_modern_client_propagates_transport_errors(mod):
    client = mod.MCPClient(URL, protocol="modern")
    _stub_urlopen(mod, [_http_error(mod, 500, "boom")])
    with pytest.raises(RuntimeError, match="HTTP 500"):
        client.connect()


def test_discover_switches_to_the_advertised_version(mod):
    client = mod.MCPClient(URL, protocol="modern")
    _stub_urlopen(
        mod,
        [
            _Response(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "supportedVersions": ["2027-01-01"],
                            "capabilities": {"tools": {}},
                            "ttlMs": 1000,
                            "cacheScope": "private",
                        },
                    }
                )
            )
        ],
    )
    assert client.connect()["protocolVersion"] == "2027-01-01"


def test_tool_color_returns_none_for_unannotated_tool(mod):
    assert mod.tool_color({"annotations": {"idempotentHint": True}}) is None


def test_error_status_with_a_non_error_json_body_is_returned(mod):
    """Some proxies answer 4xx with a valid JSON-RPC result body."""
    client = mod.MCPClient(URL, protocol="legacy")
    _stub_urlopen(
        mod, [_http_error(mod, 400, {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})]
    )
    assert client._rpc("ping") == {"ok": True}


def test_auto_reports_version_mismatch_from_a_modern_server(mod):
    """The server offers modern versions, so this is not a legacy fallback."""
    client = mod.MCPClient(URL, protocol="auto")
    client.protocol_version = "2099-01-01"
    # A fresh error per attempt: the client retries once with the advertised
    # version, and the body of a consumed HTTPError cannot be read twice.
    _stub_urlopen(
        mod,
        [
            lambda: _http_error(
                mod,
                400,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {
                        "code": -32022,
                        "message": "Unsupported protocol version",
                        "data": {
                            "supported": ["2026-07-28"],
                            "requested": "2099-01-01",
                        },
                    },
                },
            )
        ],
    )
    with pytest.raises(mod.MCPError) as excinfo:
        client.connect()
    assert excinfo.value.code == -32022


def test_tool_color_ignores_non_dict_annotations(mod):
    assert mod.tool_color({"annotations": "nope"}) is None


def test_worker_reports_connection_errors(mod):
    _assert_worker_failure(
        mod,
        lambda: (_ for _ in ()).throw(urllib.error.URLError("refused")),
        lambda failure: "Connection failed" in failure,
    )


def test_worker_forwards_mcp_errors_as_objects(mod):
    err = mod.MCPError(-32020, "bad header")
    _assert_worker_failure(
        mod,
        lambda: (_ for _ in ()).throw(err),
        lambda failure: failure is err,
    )


def test_worker_stringifies_other_exceptions(mod):
    _assert_worker_failure(
        mod,
        lambda: (_ for _ in ()).throw(ValueError("boom")),
        lambda failure: failure == "boom",
    )


def test_worker_emits_results(mod):
    _assert_worker_result(mod, lambda: {"ok": True}, {"ok": True})


def _drain(app, predicate, timeout=5.0):
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline and not predicate():
        app.processEvents()
        _time.sleep(0.01)


def _worker_app():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _assert_worker_failure(mod, fn, check):
    app = _worker_app()
    worker = mod._Worker()
    seen = []
    worker.failed.connect(seen.append)
    worker.run_async(fn)
    _drain(app, lambda: bool(seen))
    assert seen and check(seen[0])


def _assert_worker_result(mod, fn, expected):
    app = _worker_app()
    worker = mod._Worker()
    seen = []
    worker.finished.connect(seen.append)
    worker.run_async(fn)
    _drain(app, lambda: bool(seen))
    assert seen == [expected]

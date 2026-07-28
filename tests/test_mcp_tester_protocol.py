"""
Tests for mcp_gui_tester's dual-era protocol support (2026-07-28 + legacy),
tool annotations, and the protocol selector in the GUI.

Reuses the harness from test_mcp_tester.py: real in-process MCPHttpServer,
real PyQt6 offscreen, skipped when PyQt6 is unavailable.
"""

from __future__ import annotations

import json
import time

import pytest

from test_mcp_tester import (
    _StubBridge,
    _free_port,
    _has_pyqt6,
    _load_tester,
)


# ---------------------------------------------------------------------------
# Protocol helpers (pure functions)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")
class TestProtocolHelpers:
    @classmethod
    def setup_class(cls) -> None:
        cls.mod = _load_tester()

    def test_plain_ascii_header_value_is_untouched(self) -> None:
        assert self.mod.encode_header_value("get_app_info") == "get_app_info"

    def test_non_ascii_header_value_is_encoded(self) -> None:
        encoded = self.mod.encode_header_value("Hello, 世界")
        assert encoded.startswith("=?base64?") and encoded.endswith("?=")

    def test_padded_header_value_is_encoded(self) -> None:
        assert self.mod.encode_header_value(" padded ").startswith("=?base64?")

    def test_sentinel_lookalike_is_encoded(self) -> None:
        encoded = self.mod.encode_header_value("=?base64?literal?=")
        assert encoded != "=?base64?literal?="
        assert encoded.startswith("=?base64?")

    def test_control_characters_are_encoded(self) -> None:
        assert self.mod.encode_header_value("line1\nline2").startswith("=?base64?")

    def test_mcp_error_details_include_code_and_data(self) -> None:
        err = self.mod.MCPError(-32022, "Unsupported", {"supported": ["2026-07-28"]}, 400)
        text = err.details()
        assert "-32022" in text and "400" in text and "2026-07-28" in text

    def test_mcp_error_details_without_data(self) -> None:
        assert "HTTP status" not in self.mod.MCPError(-1, "boom").details()

    def test_format_annotations_read_only(self) -> None:
        tool = {"annotations": {"readOnlyHint": True, "openWorldHint": False}}
        assert self.mod.format_annotations(tool) == "read-only"

    def test_format_annotations_destructive_and_network(self) -> None:
        tool = {
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": True,
                "openWorldHint": True,
            }
        }
        assert self.mod.format_annotations(tool) == "destructive, network"

    def test_format_annotations_idempotent(self) -> None:
        tool = {"annotations": {"readOnlyHint": False, "idempotentHint": True}}
        assert self.mod.format_annotations(tool) == "idempotent"

    def test_format_annotations_missing_or_invalid(self) -> None:
        assert self.mod.format_annotations({}) == ""
        assert self.mod.format_annotations({"annotations": "nope"}) == ""

    def test_tool_color_by_annotation(self) -> None:
        assert self.mod.tool_color({"annotations": {"destructiveHint": True}})
        assert self.mod.tool_color({"annotations": {"readOnlyHint": True}})
        assert self.mod.tool_color({"annotations": {}}) is None
        assert self.mod.tool_color({}) is None

    def test_is_destructive(self) -> None:
        assert self.mod.is_destructive({"annotations": {"destructiveHint": True}})
        assert not self.mod.is_destructive({"annotations": {"readOnlyHint": True}})
        assert not self.mod.is_destructive({})


# ---------------------------------------------------------------------------
# Dual-era client against real servers in each protocol mode
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")
class TestProtocolEras:
    @classmethod
    def setup_class(cls) -> None:
        from mcp_server.server import MCPHttpServer

        cls.mod = _load_tester()
        cls.servers = {}
        cls.ports = {}
        for mode in ("auto", "legacy", "modern"):
            port = _free_port()
            server = MCPHttpServer(
                _StubBridge(), f"Stub {mode}", "0.0", port=port, protocol_mode=mode
            )
            server.start()
            cls.servers[mode] = server
            cls.ports[mode] = port
        time.sleep(0.3)

    @classmethod
    def teardown_class(cls) -> None:
        for server in cls.servers.values():
            server.stop()

    def _client(self, server_mode: str, protocol: str = "auto"):
        return self.mod.MCPClient(
            f"http://127.0.0.1:{self.ports[server_mode]}/mcp", protocol=protocol
        )

    def _capture_requests(self, client, fn):
        """Run *fn* while recording the outgoing urllib requests."""
        captured = []
        real_urlopen = self.mod.urllib.request.urlopen

        def _capturing_urlopen(req, *args, **kwargs):
            captured.append(
                {
                    "headers": {k.lower(): v for k, v in req.header_items()},
                    "body": json.loads(req.data.decode("utf-8")),
                }
            )
            return real_urlopen(req, *args, **kwargs)

        self.mod.urllib.request.urlopen = _capturing_urlopen
        try:
            fn()
        finally:
            self.mod.urllib.request.urlopen = real_urlopen
        return captured

    def test_auto_client_prefers_modern_on_dual_era_server(self) -> None:
        client = self._client("auto")
        info = client.connect()
        assert info["era"] == "modern"
        assert info["protocolVersion"] == self.mod.MODERN_PROTOCOL_VERSION
        assert info["serverInfo"]["name"] == "Stub auto"
        assert "MoleditPy" in info["instructions"]
        assert client.session_id is None

    def test_auto_client_falls_back_on_legacy_only_server(self) -> None:
        client = self._client("legacy")
        info = client.connect()
        assert info["era"] == "legacy"
        assert info["serverInfo"]["name"] == "Stub legacy"
        assert client.session_id

    def test_modern_client_against_modern_server(self) -> None:
        client = self._client("modern", protocol="modern")
        info = client.connect()
        assert info["era"] == "modern"
        assert info["supportedVersions"] == [self.mod.MODERN_PROTOCOL_VERSION]

    def test_modern_client_refuses_to_fall_back(self) -> None:
        client = self._client("legacy", protocol="modern")
        with pytest.raises(self.mod.MCPError) as excinfo:
            client.connect()
        assert excinfo.value.code == -32022
        assert excinfo.value.http_status == 400

    def test_legacy_client_against_modern_only_server(self) -> None:
        client = self._client("modern", protocol="legacy")
        with pytest.raises(self.mod.MCPError) as excinfo:
            client.connect()
        assert excinfo.value.code == -32601
        assert self.mod.MODERN_PROTOCOL_VERSION in excinfo.value.message

    def test_modern_tools_list_and_call(self) -> None:
        client = self._client("modern", protocol="modern")
        client.connect()
        tools = client.list_tools()
        assert any(t["name"] == "get_app_info" for t in tools)
        result = client.call_tool("get_app_info", {})
        assert "MoleditPy-stub" in result["content"][0]["text"]

    def test_legacy_tools_list_and_call(self) -> None:
        client = self._client("legacy", protocol="legacy")
        client.connect()
        assert client.list_tools()
        assert not client.call_tool("get_app_info", {}).get("isError")

    def test_modern_requests_mirror_the_required_headers(self) -> None:
        client = self._client("modern", protocol="modern")
        client.connect()
        captured = self._capture_requests(
            client, lambda: client.call_tool("get_app_info", {})
        )
        headers = captured[0]["headers"]
        assert headers["mcp-protocol-version"] == self.mod.MODERN_PROTOCOL_VERSION
        assert headers["mcp-method"] == "tools/call"
        assert headers["mcp-name"] == "get_app_info"

    def test_modern_requests_carry_meta(self) -> None:
        client = self._client("modern", protocol="modern")
        client.connect()
        captured = self._capture_requests(client, client.list_tools)
        meta = captured[0]["body"]["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == (
            self.mod.MODERN_PROTOCOL_VERSION
        )
        assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "mcp-gui-tester"
        assert meta["io.modelcontextprotocol/clientCapabilities"] == {}

    def test_legacy_client_echoes_session_id(self) -> None:
        client = self._client("legacy", protocol="legacy")
        client.connect()
        captured = self._capture_requests(client, client.list_tools)
        assert captured[0]["headers"]["mcp-session-id"] == client.session_id

    def test_unsupported_version_triggers_retry_with_advertised_version(self) -> None:
        client = self._client("auto", protocol="modern")
        client.protocol_version = "2099-01-01"  # rejected by the server
        info = client.connect()
        assert info["protocolVersion"] == self.mod.MODERN_PROTOCOL_VERSION

    def test_ping_works_in_both_eras(self) -> None:
        modern = self._client("modern", protocol="modern")
        modern.connect()
        # Modern responses carry serverInfo in the result's _meta.
        assert "io.modelcontextprotocol/serverInfo" in modern.ping()["_meta"]
        legacy = self._client("legacy", protocol="legacy")
        legacy.connect()
        assert legacy.ping() == {}

    def test_summary_reports_connection_state(self) -> None:
        client = self._client("auto")
        client.connect()
        summary = client.summary()
        assert summary["era"] == "modern"
        assert summary["capabilities"] == {"tools": {}}

    def test_invalid_protocol_mode_falls_back_to_auto(self) -> None:
        assert self._client("auto", protocol="bogus").protocol == "auto"

    def test_non_json_http_error_is_reported_plainly(self) -> None:
        client = self.mod.MCPClient(
            f"http://127.0.0.1:{self.ports['auto']}/nope", protocol="legacy"
        )
        with pytest.raises(RuntimeError, match="HTTP 404"):
            client.connect()


# ---------------------------------------------------------------------------
# GUI: protocol selector, annotations, destructive confirmation
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_pyqt6(), reason="PyQt6 not installed")
class TestTesterGUIProtocol:
    @classmethod
    def setup_class(cls) -> None:
        from PyQt6.QtWidgets import QApplication

        from mcp_server.server import MCPHttpServer

        cls.mod = _load_tester()
        cls.app = QApplication.instance() or QApplication([])
        cls.port = _free_port()
        cls.server = MCPHttpServer(_StubBridge(), "Stub MCP", "0.0", port=cls.port)
        cls.server.start()
        time.sleep(0.2)
        cls.client = cls.mod.MCPClient(f"http://127.0.0.1:{cls.port}/mcp")
        cls.client.connect()
        cls.tools = cls.client.list_tools()

    @classmethod
    def teardown_class(cls) -> None:
        cls.server.stop()

    def _make_window(self, protocol: str = "auto"):
        win = self.mod.MCPTesterWindow(
            f"http://127.0.0.1:{self.port}/mcp", protocol=protocol
        )
        win.tools = self.tools
        win._populate_tool_list()
        return win

    def _select(self, win, tool_name: str) -> None:
        names = [t["name"] for t in self.tools]
        win.tool_list.setCurrentRow(names.index(tool_name))
        self.app.processEvents()

    def test_protocol_combo_lists_every_mode(self) -> None:
        win = self._make_window()
        modes = [
            win.protocol_combo.itemData(i) for i in range(win.protocol_combo.count())
        ]
        assert modes == ["auto", "modern", "legacy"]

    def test_protocol_combo_defaults_to_auto(self) -> None:
        assert self._make_window().protocol_combo.currentData() == "auto"

    def test_protocol_combo_honours_cli_argument(self) -> None:
        assert self._make_window("modern").protocol_combo.currentData() == "modern"

    def test_unknown_cli_protocol_falls_back_to_auto(self) -> None:
        assert self._make_window("bogus").protocol_combo.currentData() == "auto"

    def test_connect_uses_selected_protocol(self) -> None:
        win = self._make_window("legacy")
        win._on_connect()
        assert win.client is not None
        assert win.client.protocol == "legacy"

    def test_tool_list_shows_annotation_hints_in_tooltip(self) -> None:
        win = self._make_window()
        names = [t["name"] for t in self.tools]
        assert "destructive" in win.tool_list.item(names.index("delete_file")).toolTip()
        assert "read-only" in win.tool_list.item(names.index("get_app_info")).toolTip()

    def test_description_shows_annotation_hints(self) -> None:
        win = self._make_window()
        self._select(win, "get_app_info")
        assert "read-only" in win.desc_label.text()

    def test_destructive_call_is_confirmed(self, monkeypatch) -> None:
        from PyQt6.QtWidgets import QMessageBox

        win = self._make_window()
        win.client = self.client
        self._select(win, "clear_canvas")
        asked = []
        monkeypatch.setattr(
            self.mod.QMessageBox,
            "question",
            lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.Cancel)[1],
        )
        win._on_call()
        assert asked, "destructive tool must prompt before calling"
        assert win.status_label.text() == "Call cancelled"

    def test_destructive_confirmation_can_be_disabled(self, monkeypatch) -> None:
        win = self._make_window()
        win.client = self.client
        self._select(win, "clear_canvas")
        win.confirm_destructive_chk.setChecked(False)
        asked = []
        monkeypatch.setattr(
            self.mod.QMessageBox, "question", lambda *a, **k: asked.append(a)
        )
        win._on_call()
        assert not asked

    def test_read_only_call_is_not_confirmed(self, monkeypatch) -> None:
        win = self._make_window()
        win.client = self.client
        self._select(win, "get_app_info")
        asked = []
        monkeypatch.setattr(
            self.mod.QMessageBox, "question", lambda *a, **k: asked.append(a)
        )
        win._on_call()
        assert not asked

    def test_server_tab_summarizes_the_connection(self) -> None:
        win = self._make_window()
        win._on_connected({"info": self.client.summary(), "tools": self.tools})
        text = win.server_text.toPlainText()
        assert "Era:" in text and "modern" in text
        assert "instructions:" in text
        assert "modern" in win.status_label.text()

    def test_error_dialog_shows_jsonrpc_details(self, monkeypatch) -> None:
        shown = []
        monkeypatch.setattr(
            self.mod.QMessageBox, "critical", lambda *a, **k: shown.append(a[2])
        )
        win = self._make_window()
        win._on_error(self.mod.MCPError(-32020, "Header mismatch", None, 400))
        assert "-32020" in shown[0]
        assert "-32020" in win.status_label.text()

    def test_error_dialog_accepts_plain_strings(self, monkeypatch) -> None:
        shown = []
        monkeypatch.setattr(
            self.mod.QMessageBox, "critical", lambda *a, **k: shown.append(a[2])
        )
        win = self._make_window()
        win._on_error("Connection failed: refused")
        assert shown == ["Connection failed: refused"]
        assert win.status_label.text() == "Error"

    def test_call_reports_elapsed_time(self) -> None:
        win = self._make_window()
        win._call_started = time.monotonic()
        win._on_call_done({"content": [{"type": "text", "text": "ok"}]})
        assert "ms" in win.status_label.text()
        assert win._call_started is None

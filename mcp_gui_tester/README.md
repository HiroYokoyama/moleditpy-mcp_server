# mcp-gui-tester

[![PyPI](https://img.shields.io/pypi/v/mcp-gui-tester)](https://pypi.org/project/mcp-gui-tester/) [![Python](https://img.shields.io/pypi/pyversions/mcp-gui-tester)](https://pypi.org/project/mcp-gui-tester/) [![Tests](https://github.com/HiroYokoyama/moleditpy-mcp_server/actions/workflows/test.yml/badge.svg)](https://github.com/HiroYokoyama/moleditpy-mcp_server/actions/workflows/test.yml)

A lightweight **PyQt6 GUI tester for MCP servers** speaking the
[Model Context Protocol](https://modelcontextprotocol.io) **Streamable HTTP
transport** (JSON-RPC over `POST`).

Connect to a running server, browse its tools, fill in parameters through a
form generated from each tool's `inputSchema`, call the tool, and inspect the
result — no Node toolchain, no browser, just `pip install`.

Developed alongside the [MoleditPy MCP Server](https://github.com/HiroYokoyama/moleditpy-mcp_server)
plugin, but fully generic: it works with any MCP server exposing tools over
Streamable HTTP.

## Installation

```bash
pip install mcp-gui-tester
```

## Usage

```bash
mcp-gui-tester                                  # defaults to http://127.0.0.1:7891/mcp
mcp-gui-tester --url http://localhost:9000/mcp  # any MCP HTTP endpoint
mcp-gui-tester --protocol modern                # force MCP 2026-07-28 (stateless)
python -m mcp_gui_tester                        # equivalent
```

1. Adjust **Host / Port / Path / Protocol** (and optional **Headers**, a JSON
   object like `{"Authorization": "Bearer ..."}`) and click **Connect** — the
   status bar shows the server name, version, negotiated protocol era, and
   tool count, and the **Server** tab shows the full handshake result.
2. Select a tool from the filterable list. If you've called it before, the
   form is prefilled with the last arguments you sent; click **Reset form**
   to clear back to the schema defaults.
3. Fill in the parameters and click **Call Tool**.
4. Inspect the formatted **Result** tab (including inline images and
   pretty-printed resource/unknown content blocks) or the **Raw JSON** tab.
5. Click **Refresh tools** at any time to re-fetch `tools/list` on the
   current connection without losing your place in the tool list.

## Features

- **Both protocol eras** — `2026-07-28` (stateless: per-request `_meta`, mirrored
  `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` headers with Base64 sentinel
  encoding, `server/discover`) and the `initialize` handshake era (with
  `Mcp-Session-Id` echo). **Auto-detect** probes with `server/discover` and falls
  back to the handshake; an `UnsupportedProtocolVersion` error is honoured by
  retrying with a version the server advertises.
- **Server tab** — negotiated era and version, supported versions, session id,
  `serverInfo`, capabilities, and the server's natural-language `instructions`
- **Tool browser** — lists every tool from `tools/list` with its description;
  filter by name or description text; **Refresh tools** re-fetches the list
  on the existing connection, preserving the current selection
- **Annotation-aware** — tools are coloured and labelled by their
  `annotations` (read-only / destructive / idempotent / network), and a
  destructive tool asks for confirmation before it is called (toggleable)
- **Schema-driven parameter forms**, generated from each tool's `inputSchema`:
  - strings → line edit (multi-line editor for code / file-content / XYZ / MOL
    block parameters)
  - integer / number → spinbox, boolean → checkbox
  - array / object → JSON editor, validated before sending
  - `enum` → dropdown, `default` values pre-filled
  - `oneOf` unions → a widget chosen from the alternatives (array/object get
    a JSON-capable multiline editor, string gets a line/multiline edit,
    otherwise the first alternative's own widget is used); on submit, text
    that looks like JSON and parses to one of the allowed types is sent as
    that type, otherwise it's sent as a plain string
  - optional parameters carry a **send** checkbox and are omitted unless
    checked; required parameters are marked with `*`
- **Per-tool argument memory** — the last arguments sent to each tool are
  remembered for the current session (in memory only — nothing is written
  to disk) and used to prefill the form next time you select that tool;
  **Reset form** clears the prefill back to schema defaults.
- **Custom headers** — an optional JSON object of extra HTTP headers (e.g.
  bearer tokens) sent with every request on the connection; invalid JSON is
  rejected with a clear error instead of crashing
- **Result view** — formatted text (tool errors flagged with `[TOOL ERROR]`),
  inline rendering of `image` content blocks, pretty-printed JSON for
  `resource` and other content types, plus the raw JSON-RPC response and the
  round-trip time of the call
- **Real error reporting** — JSON-RPC errors returned with an HTTP error status
  (e.g. `400` header/version failures in the 2026-07-28 era) are shown with
  their code, message, and `data` instead of a bare "HTTP Error 400"
- **Responsive** — calls run on a background thread, so slow tools never
  freeze the GUI

## Scope

This is deliberately a small tool. It supports the Streamable HTTP transport
and the `tools/*` capability (`server/discover` / `initialize`, `tools/list`,
`tools/call`, `ping`), plus static bearer/custom headers for simple
authentication schemes.
It does not currently speak stdio transport, SSE streaming, OAuth, or the
resources/prompts capabilities. For a full-featured inspector, see the
official [MCP Inspector](https://github.com/modelcontextprotocol/inspector).

## Requirements

- Python 3.9+
- PyQt6 (installed automatically)

## License

GPL-3.0-only — see [LICENSE](LICENSE).

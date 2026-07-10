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
python -m mcp_gui_tester                        # equivalent
```

1. Adjust **Host / Port / Path** (and optional **Headers**, a JSON object
   like `{"Authorization": "Bearer ..."}`) and click **Connect** — the status
   bar shows the server name, version, and tool count.
2. Select a tool from the filterable list. If you've called it before, the
   form is prefilled with the last arguments you sent; click **Reset form**
   to clear back to the schema defaults.
3. Fill in the parameters and click **Call Tool**.
4. Inspect the formatted **Result** tab (including inline images and
   pretty-printed resource/unknown content blocks) or the **Raw JSON** tab.
5. Click **Refresh tools** at any time to re-fetch `tools/list` on the
   current connection without losing your place in the tool list.

## Features

- **Tool browser** — lists every tool from `tools/list` with its description;
  filter by name or description text; **Refresh tools** re-fetches the list
  on the existing connection, preserving the current selection
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
  remembered (in-memory and persisted to `~/.mcp_gui_tester_history.json`,
  capped at the last 50 tools) and used to prefill the form next time you
  select that tool; **Reset form** clears the prefill back to schema
  defaults. A corrupt or unreadable history file is ignored silently.
- **Custom headers** — an optional JSON object of extra HTTP headers (e.g.
  bearer tokens) sent with every request on the connection; invalid JSON is
  rejected with a clear error instead of crashing
- **Result view** — formatted text (tool errors flagged with `[TOOL ERROR]`),
  inline rendering of `image` content blocks, pretty-printed JSON for
  `resource` and other content types, plus the raw JSON-RPC response
- **Responsive** — calls run on a background thread, so slow tools never
  freeze the GUI

## Scope

This is deliberately a small tool. It supports the Streamable HTTP transport
and the `tools/*` capability (`initialize`, `tools/list`, `tools/call`), plus
static bearer/custom headers for simple authentication schemes.
It does not currently speak stdio transport, SSE streaming, OAuth, or the
resources/prompts capabilities. For a full-featured inspector, see the
official [MCP Inspector](https://github.com/modelcontextprotocol/inspector).

## Requirements

- Python 3.9+
- PyQt6 (installed automatically)

## License

GPL-3.0-only — see [LICENSE](LICENSE).

# mcp-gui-tester

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

1. Adjust **Host / Port / Path** if needed and click **Connect** — the status
   bar shows the server name, version, and tool count.
2. Select a tool from the filterable list.
3. Fill in the parameters and click **Call Tool**.
4. Inspect the formatted **Result** tab or the **Raw JSON** tab.

## Features

- **Tool browser** — lists every tool from `tools/list` with its description;
  filter by name or description text
- **Schema-driven parameter forms**, generated from each tool's `inputSchema`:
  - strings → line edit (multi-line editor for code / file-content / XYZ / MOL
    block parameters)
  - integer / number → spinbox, boolean → checkbox
  - array / object → JSON editor, validated before sending
  - `enum` → dropdown, `default` values pre-filled
  - optional parameters carry a **send** checkbox and are omitted unless
    checked; required parameters are marked with `*`
- **Result view** — formatted text (tool errors flagged with `[TOOL ERROR]`)
  plus the raw JSON-RPC response
- **Responsive** — calls run on a background thread, so slow tools never
  freeze the GUI

## Scope

This is deliberately a small tool. It supports the Streamable HTTP transport
and the `tools/*` capability (`initialize`, `tools/list`, `tools/call`).
It does not currently speak stdio transport, SSE streaming, authentication,
or the resources/prompts capabilities. For a full-featured inspector, see the
official [MCP Inspector](https://github.com/modelcontextprotocol/inspector).

## Requirements

- Python 3.9+
- PyQt6 (installed automatically)

## License

GPL-3.0-only — see [LICENSE](LICENSE).

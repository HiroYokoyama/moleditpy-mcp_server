# tools/

Developer utilities for the MoleditPy MCP Server. These are standalone
scripts — they are not part of the plugin and are never loaded by MoleditPy.

## mcp_tester.py — GUI test client

A standalone PyQt6 GUI for interactively testing any MCP server that speaks
the **Streamable HTTP transport** (JSON-RPC over `POST`). It is developed
against the MoleditPy MCP Server plugin but is fully generic.

Layout: tool list with filter (left) · description, parameter form, and
result tabs (right).

### Features

- **Connect** to any server via editable **Host / Port / Path** fields
  (default `127.0.0.1` / `7891` / `/mcp`)
- **Tool browser** — lists every tool from `tools/list` with its
  description; type in the filter box to narrow by name or description
- **Schema-driven parameter forms** — generated from each tool's
  `inputSchema`:
  - strings → line edit (multi-line editor for code/content/XYZ/MOL blocks)
  - integer / number → spinbox, boolean → checkbox
  - array / object → JSON editor with validation before sending
  - `enum` → dropdown, `default` values pre-filled
  - optional parameters have a **send** checkbox and are omitted unless
    checked; required parameters are marked with `*`
- **Results** — a formatted **Result** tab (tool errors flagged with
  `[TOOL ERROR]`) and a **Raw JSON** tab with the full response
- Calls run on a background thread, so the GUI stays responsive during
  slow tools (e.g. `run_python`, `get_plugin_dev_manual`)

### Usage

```bash
# Start MoleditPy with the MCP Server plugin running, then:
python tools/mcp_tester.py

# Or point it at any other MCP HTTP server:
python tools/mcp_tester.py --url http://localhost:9000/mcp
```

1. Click **Connect** — the status bar shows the server name, version, and
   tool count.
2. Select a tool from the list, fill in the parameters.
3. Click **Call Tool** and inspect the Result / Raw JSON tabs.

### Requirements

- Python 3.9+
- PyQt6 (already present wherever MoleditPy is installed)

No other dependencies — HTTP is handled with the standard library.

### Tests

The tester is covered by `tests/test_mcp_tester.py` (pure helpers, the
JSON-RPC client against a real in-process `MCPHttpServer`, and offscreen
GUI tests):

```bash
python -m pytest tests/test_mcp_tester.py -v
```

The GUI tests skip automatically when PyQt6 is not installed.

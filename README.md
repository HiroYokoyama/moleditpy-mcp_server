# MoleditPy MCP Server Plugin

Expose [MoleditPy](https://github.com/HiroYokoyama/python_molecular_editor) to AI assistants via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io).

Once running, any MCP-compatible client — **Claude Desktop**, **Claude Code**, or any HTTP client — can query and control the molecular editor in real time.

[![Tests](https://github.com/HiroYokoyama/moleditpy-mcp_server/actions/workflows/test.yml/badge.svg)](https://github.com/HiroYokoyama/moleditpy-mcp_server/actions/workflows/test.yml)

---

## Installation

1. **Copy** (or symlink) the `mcp_server/` folder into your MoleditPy plugin directory:

   | Platform | Path |
   |----------|------|
   | Windows | `C:\Users\<You>\.moleditpy\plugins\mcp_server\` |
   | Linux / macOS | `~/.moleditpy/plugins/mcp_server/` |

2. **Restart MoleditPy** (or choose **Plugins → Reload All Plugins**).

3. Choose **Plugins → MCP Server → Status & Settings…** to start the server.

---

## Usage

### Starting the server

Open **Plugins → MCP Server → Status & Settings…**, set the port (default **7891**), and click **Start Server**.

The dialog shows the live server URL and a ready-to-paste configuration snippet.

---

## Connecting MCP clients

The server implements **MCP Streamable HTTP** (`POST /mcp`, protocol version `2024-11-05`).

### Claude Desktop

Add the following to `claude_desktop_config.json` (replace the port if you changed it):

```json
{
  "mcpServers": {
    "moleditpy": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:7891/mcp"
    }
  }
}
```

Restart Claude Desktop. MoleditPy now appears as a connected tool server.

### Claude Code (CLI)

Add the server to your Claude Code MCP configuration:

```json
{
  "mcpServers": {
    "moleditpy": {
      "type": "http",
      "url": "http://127.0.0.1:7891/mcp"
    }
  }
}
```

Or set it per-project in `.claude/settings.json` inside your project folder.

### curl / raw HTTP

You can call the server from any HTTP client. Example — list available tools:

```bash
curl -s -X POST http://127.0.0.1:7891/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python -m json.tool
```

Call a tool:

```bash
curl -s -X POST http://127.0.0.1:7891/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_current_molecule","arguments":{}}}' | python -m json.tool
```

Health check:

```bash
curl http://127.0.0.1:7891/health
```

### Auto-start

To start the server automatically every time MoleditPy launches, run this once in the **Python Console** plugin:

```python
context.set_setting("auto_start", True)
```

---

## Available MCP Tools

### Molecule tools

| Tool | Description |
|------|-------------|
| `get_current_molecule` | SMILES, formula, MW, atom/bond counts, 3D availability |
| `get_molecule_xyz` | 3D XYZ coordinate block (`Element X Y Z` per line) |
| `get_atom_properties` | Per-atom: symbol, Z, charge, hybridization, Hs, radical electrons |
| `get_bond_info` | Full bond table: endpoints and bond type (SINGLE/DOUBLE/TRIPLE/AROMATIC) |
| `get_selected_atoms` | Indices and symbols of user-selected atoms |
| `load_molecule_from_smiles` | Draw a molecule from a SMILES string |
| `load_from_mol_block` | Load a molecule from a MOL/SDF block |
| `show_xyz_in_viewer` | Display an XYZ block in the 3D viewer |
| `trigger_3d_conversion` | Run MoleditPy's built-in 2D→3D optimizer (ETKDG/MMFF) |
| `highlight_atoms` | Override atom colors in the 3D viewer (hex color per atom index) |
| `clear_canvas` | Clear the 2D editor (undo-safe) |
| `get_app_info` | MoleditPy version and MCP plugin version |

### File I/O tools (sandboxed)

| Tool | Description |
|------|-------------|
| `write_text_file` | Write text to a file; auto-creates parent dirs; `overwrite=false` by default |
| `read_text_file` | Read a file's UTF-8 text content (≤ 4 MB) |
| `list_directory` | List files and subdirectories with sizes |
| `delete_file` | Delete a file; requires explicit `confirm=true` |
| `get_file_io_config` | Show current base directory and allowed extension list |
| `set_file_io_config` | Set the sandbox directory and/or update the extension allowlist |

#### Security model

All file operations are restricted to a **base directory** you configure:

```python
# In MoleditPy's Python Console plugin — run once to configure
context.set_setting("file_io_base_dir", "/home/you/dft_jobs")
```

Or let the LLM set it via `set_file_io_config`:

```
Set the file I/O base directory to /home/you/dft_jobs
```

Security guarantees:
- **Path traversal blocked** — `../../etc/passwd` and absolute paths are rejected; every path is resolved and must stay within the base directory.
- **Extension allowlist** — only extensions on the allowed list can be written/read/deleted. Defaults cover common DFT/QM formats (`.inp`, `.xyz`, `.gjf`, `.mol`, `.pdb`, `.txt`, `.json`, …). Use `set_file_io_config` to customise.
- **Overwrite protection** — `write_text_file` refuses to replace existing files unless `overwrite=true` is passed explicitly.
- **Deletion requires confirmation** — `delete_file` requires `confirm=true` in the same call.
- **Size limit** — reads and writes are capped at 4 MB.

#### Typical DFT workflow example

> "Generate an ORCA input file for the current molecule using B3LYP/def2-TZVP and save it to `ethanol_opt.inp`."

The LLM will:
1. Call `get_molecule_xyz` to get the geometry.
2. Call `write_text_file` with `path="ethanol_opt.inp"` and the generated ORCA input.
3. Optionally call `read_text_file` to confirm what was written.

---

## Architecture

```
mcp_server/
├── __init__.py   — Plugin entry point (initialize, MCPServerPlugin)
├── bridge.py     — Thread-safe Qt signal bridge (server thread → Qt main thread)
├── server.py     — HTTP server implementing MCP Streamable HTTP transport
└── ui.py         — Status & Settings dialog
```

**Thread safety** — All PluginContext calls must occur on the Qt main thread. `MCPBridge` achieves this by emitting a `QueuedConnection` signal from the server thread; the main thread executes the operation and signals completion via a `threading.Event`.

**No extra dependencies** — Uses only Python's built-in `http.server` and `threading`.

---

## Development

```bash
# Install test dependencies
pip install pytest pytest-cov

# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=mcp_server --cov-report=term-missing
```

Tests run fully headlessly — no GUI, no RDKit, no MoleditPy installation required.

---

## Compatibility

| Requirement | Version |
|-------------|---------|
| MoleditPy | ≥ 4.0.0, < 5.0.0 |
| Python | 3.11+ |
| MCP protocol | 2024-11-05 (Streamable HTTP) |

No extra pip dependencies — uses Python's built-in `http.server` and `threading`.

# CLAUDE.md — moleditpy-mcp_server

MCP Server plugin for MoleditPy. Exposes the molecular editor's operations via the Model Context Protocol (MCP) over a local HTTP connection, enabling AI assistants (Claude Desktop, etc.) to query and control the editor.

## Installation

Copy (or symlink) `mcp_server/` to the MoleditPy plugin directory:

- **Windows:** `C:\Users\<You>\.moleditpy\plugins\mcp_server\`
- **Linux/macOS:** `~/.moleditpy/plugins/mcp_server/`

No `pip install` is needed — the plugin has no extra dependencies beyond MoleditPy itself (uses `starlette` + `uvicorn` bundled with or installed alongside the app).

## Running Tests

```bash
# Unit + bridge tests (no MoleditPy install needed)
pytest tests/ -v

# With coverage
pytest tests/ -v --cov=mcp_server --cov-report=term-missing

# Single test file
pytest tests/test_bridge.py -v
```

All tests run headlessly. `tests/conftest.py` mocks PyQt6, RDKit, pyvista, and `moleditpy` at the import level via a custom `MetaPathFinder`, so no real MoleditPy install is required for the unit suite.

## Architecture

| Module | Role |
|---|---|
| `mcp_server/__init__.py` | Plugin entry point — `initialize(context)`, `MCPServerPlugin` lifecycle (start/stop) |
| `mcp_server/bridge.py` | `MCPBridge` — routes MCP tool calls to MoleditPy operations via `PluginContext`; all tool handlers live here |
| `mcp_server/server.py` | `MCPHttpServer` — `starlette` / SSE transport; runs on a `QThread` |
| `mcp_server/ui.py` | `MCPStatusDialog` — Qt settings dialog (port, auto-start, file I/O base dir) |

The plugin follows the standard MoleditPy plugin contract: `initialize(context: PluginContext)` registers menu items and reads/writes settings through `context.get_setting` / `context.set_setting`.

## Key Settings

All settings are stored under `plugin.mcp_server.<key>` in the app's persistent settings (namespaced automatically by `PluginContext`). Setting keys used by this plugin:

| Key | Type | Default | Where set |
|---|---|---|---|
| `auto_start` | bool | `False` | GUI checkbox (Status & Settings dialog) |
| `port` | int | `7891` | GUI port spinner |
| `file_io_base_dir` | str or None | `None` (unrestricted) | GUI browse field or `set_file_io_config` MCP tool |
| `file_io_allowed_extensions` | list[str] | see `_DEFAULT_EXTENSIONS` | `set_file_io_config` MCP tool |

## Adding New MCP Tools

1. Add a handler function `_my_tool(ctx, args)` in `bridge.py`.
2. Register it in `_OPERATIONS` dict at the bottom of `bridge.py`.
3. Add the tool definition (name, description, input schema) to `_TOOL_DEFINITIONS` in `server.py`.
4. Add tests in `tests/test_bridge.py`.

## CI

Two jobs in `.github/workflows/test.yml`:
- `test` — runs `pytest tests/` on Python 3.11–3.13, no MoleditPy install
- `test-integration` — clones the main app (`HiroYokoyama/python_molecular_editor`) and runs the full suite against the real `PluginContext`

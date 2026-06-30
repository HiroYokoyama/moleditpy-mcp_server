# Contributing to MoleditPy MCP Server Plugin

Thank you for your interest in contributing! This plugin exposes MoleditPy to AI assistants via the Model Context Protocol. Contributions that expand its tool coverage, improve security, or increase test coverage are especially welcome.

## Reporting bugs

When opening an issue please include:

1. **Steps to reproduce** — what did you ask the AI to do, and what happened?
2. **Expected vs. actual behaviour.**
3. **MoleditPy version** and **Python version**.
4. **Console output** — the server logs at `DEBUG` level; run MoleditPy from a terminal to capture it.

## Development setup

```bash
# Clone the repo
git clone https://github.com/HiroYokoyama/moleditpy-mcp_server
cd moleditpy-mcp_server

# Install test dependencies (no MoleditPy or RDKit needed for tests)
pip install pytest pytest-cov

# Run the full test suite
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=mcp_server --cov-report=term-missing
```

Tests run fully headlessly — no GUI, no RDKit, no MoleditPy installation required.

## Project layout

| File | Role |
|------|------|
| `mcp_server/__init__.py` | Plugin entry point (`initialize`, `MCPServerPlugin`) |
| `mcp_server/bridge.py` | Thread-safe Qt signal bridge; pure-Python `execute_operation` dispatch |
| `mcp_server/server.py` | HTTP server (MCP Streamable HTTP); `dispatch_tool`; security helpers |
| `mcp_server/ui.py` | Status & Settings dialog |
| `tests/conftest.py` | `load_module`, `make_context`, `mock_optional_imports` helpers |
| `tests/test_bridge.py` | Unit tests for `execute_operation` (no Qt) |
| `tests/test_server.py` | Unit tests for `dispatch_tool` and HTTP handling |
| `tests/test_init.py` | Plugin lifecycle tests |

## Adding a new tool

1. **bridge.py** — add a branch in `execute_operation` (and a helper `_my_op` function if needed). No Qt imports at module level — keep them lazy inside the helper.
2. **server.py** — add an entry to `_TOOLS` with `name`, `description`, and `inputSchema`, then add a dispatch branch in `dispatch_tool`.
3. **tests/test_bridge.py** — test `execute_operation(ctx, "my_op", args)` directly with a `MagicMock` context.
4. **tests/test_server.py** — test `dispatch_tool(bridge, "my_tool", arguments)` with `make_bridge({"my_op": {...}})`.

## Coding standards

- **Type hints** on all public functions.
- **No Qt imports at module level** in `bridge.py` — all `from PyQt6…` must be inside method bodies so `execute_operation` stays unit-testable without a `QApplication`.
- **Error handling** — `except` blocks must log or raise; never bare `pass`.
- **Security** — file I/O tools must go through `_resolve_safe_path` and `_check_extension`. Any new network call must handle `urllib.error.HTTPError` and timeouts.

## Pull request process

1. Branch from `main` (e.g. `feature/my-new-tool`).
2. Run `pytest tests/ -v` — all tests must pass.
3. Describe what the new tool does and include a short example prompt in the PR description.
4. A maintainer will review and may request changes.

## License

By contributing you agree that your contributions will be licensed under the project's [GPL-3.0 License](./LICENSE).

---
*Happy coding!*

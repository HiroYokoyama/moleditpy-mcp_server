# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x (latest) | ✅ |

Only the latest release receives security fixes.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Send an email to: **titech.yoko.hiro[at]gmail.com**

Include:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- The version of the plugin affected
- Your OS and Python version

You can expect an acknowledgement within **5 business days**. Confirmed issues will be fixed as soon as possible and credited in the release notes (unless you prefer to remain anonymous).

## Threat model

Unlike MoleditPy itself, the MCP Server plugin **opens a local HTTP port** and executes tool calls from any client that can reach it. The primary attack surfaces are:

### Network exposure

The server binds to `127.0.0.1` (loopback only) by default. It should never be exposed to an untrusted network. There is no authentication — any process on the local machine can call the MCP endpoint.

**Risk:** A malicious local process could call any MCP tool, including `run_python`.

**Mitigation:** Keep the server on loopback. Do not port-forward or proxy it externally.

### `run_python` — arbitrary code execution

`run_python` executes arbitrary Python code on MoleditPy's Qt main thread with full `PluginContext` access. It is intentionally unrestricted — it is a power tool, not a sandboxed environment.

**Risk:** Any MCP client (or local attacker) that can reach the endpoint can execute arbitrary Python in the MoleditPy process.

**Mitigation:** Only connect trusted MCP clients (Claude Desktop, Claude Code, your own scripts). Do not expose the port outside localhost.

### File I/O sandbox

`write_text_file`, `read_text_file`, `list_directory`, and `delete_file` are restricted to a configured base directory with an extension allowlist and path-traversal checks. However, the `run_python` tool can bypass these restrictions entirely.

### Plugin reload

`reload_plugins` re-executes all `__init__.py` files in the plugin directory. If an attacker can write to that directory (via `write_text_file` or otherwise), they can achieve code execution on the next reload.

## Out of scope

- Vulnerabilities in MoleditPy itself — report those to the [main repository](https://github.com/HiroYokoyama/python_molecular_editor).
- Attacks requiring physical access to the machine.
- Attacks requiring the user to connect a malicious MCP client they chose to trust.

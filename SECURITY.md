# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| latest release | ✅ |
| older releases | ❌ |

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

The server binds to `127.0.0.1` (loopback only). It should never be exposed to an untrusted network.

Every `POST /mcp` must carry a bearer token (`Authorization: Bearer <token>`). The token is 32 random bytes generated on first start and kept in the plugin's own `settings.json`; it is compared in constant time, and a request without it is refused with 401 before its body is parsed. Another program on the machine can call the endpoint only if it can read that file, which means it already runs as you.

**The token is a credential.** The configuration snippet in **Status & Settings** contains it. Share that snippet only with clients you trust, and do not commit or paste it anywhere public.

**Mitigation:** Keep the server on loopback. Do not port-forward or proxy it externally.

### Web pages in a local browser

Any web page can send requests to `127.0.0.1`, and a DNS-rebinding page can also read the replies. The server therefore refuses (HTTP 403) any request whose `Origin` header is not a loopback origin (`localhost`, `127.0.0.1`, `[::1]`), and any request whose `Host` header does not name the address it is bound to. CORS headers echo only loopback origins, never `*`. Native MCP clients send no `Origin` and are unaffected. Request bodies are capped at 16 MB. A page that got past all of that would still need the token.

### The client you connected, and what it reads

The checks above keep strangers out. They say nothing about the assistant you connected yourself, and an assistant acts on text it reads: a structure file, a compound record, the plugin registry, a file in a read-only folder. Text written to steer it can make it call tools you did not ask for. This is the main risk of running the plugin, and it is why `run_python` has its own setting.

### `run_python` -- arbitrary code execution

`run_python` executes arbitrary Python code on MoleditPy's Qt main thread with full `PluginContext` access. It is not a sandbox: the code runs with your full access to the computer.

**Status & Settings > run_python tool** decides whether a client may use it:

| Setting | Effect |
|---|---|
| Allowed (default) | Runs without asking. The file-access settings do not limit it: the code can open any file and change those settings itself. |
| Ask each time | Each call shows its code in a MoleditPy dialog and runs only on Yes. |
| Off | Refused, and not offered to clients in the tool list. |

The setting is read in the app, on the GUI thread, before any code runs; no tool call changes it. Choose **Ask each time** or **Off** unless you need unattended code execution and trust everything the assistant will read.

### File I/O sandbox

`write_text_file`, `read_text_file`, `list_directory`, and `delete_file` are restricted to a configured base directory with an extension allowlist and path-traversal checks. Widening that access through `set_file_io_config` or `request_read_folder` needs your approval in a MoleditPy dialog. Paths that name another machine (UNC) or a device are refused before anything touches them.

This limits the file tools. It is a boundary for the client as a whole only while `run_python` is set to **Ask each time** or **Off**.

### Plugin reload

`reload_plugins` re-executes all `__init__.py` files in the plugin directory. If the base directory you approved contains the plugin folder and `.py` is on the extension list, a client can write a plugin and load it, which is code execution whatever `run_python` is set to. Do not make the plugin folder writable unless that is what you want.

## Out of scope

- Vulnerabilities in MoleditPy itself — report those to the [main repository](https://github.com/HiroYokoyama/python_molecular_editor).
- Attacks requiring physical access to the machine.
- Attacks requiring the user to connect a malicious MCP client they chose to trust.

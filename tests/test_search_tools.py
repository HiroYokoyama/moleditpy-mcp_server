"""Tests for grep_files / find_files, line-range reads, and tool annotations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import load_module, make_bridge, mock_optional_imports


@pytest.fixture()
def srv():
    with mock_optional_imports():
        yield load_module("server.py")


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A small source tree to search."""
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "molecule.py").write_text(
        "class Molecule:\n"
        "    def add_atom(self, symbol):\n"
        "        return symbol\n",
        encoding="utf-8",
    )
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "plugin_interface.py").write_text(
        "def add_menu_action(path, callback):\n"
        "    '''Register a menu item.'''\n"
        "    return None\n"
        "\n"
        "ADD_MENU_ACTION_ALIAS = add_menu_action\n",
        encoding="utf-8",
    )
    (tmp_path / "notes.md").write_text("add_menu_action is documented here\n", "utf-8")
    (tmp_path / "data.bin").write_bytes(b"add_menu_action\x00\x01binary")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.py").write_text("add_menu_action\n", "utf-8")
    return tmp_path


def _bridge_for(tree: Path, base_dir: Path | None = None, exts=None) -> Any:
    return make_bridge(
        {
            "get_app_source_root": {"root": str(tree)},
            "get_plugin_dir": {"plugin_dir": str(tree / "plugins")},
            "get_file_io_config": {
                "base_dir": str(base_dir or tree),
                "allowed_extensions": exts or [".txt", ".md"],
            },
        }
    )


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


def test_resolve_root_app_source(srv, tree):
    base, start, exts = srv._resolve_search_root(_bridge_for(tree), "app_source", "")
    assert base == tree.resolve() and start == base and exts is None


def test_resolve_root_plugins(srv, tree):
    base, start, exts = srv._resolve_search_root(_bridge_for(tree), "plugins", "")
    assert base == (tree / "plugins").resolve() and exts is None


def test_resolve_root_files_returns_allowlist(srv, tree):
    _, _, exts = srv._resolve_search_root(_bridge_for(tree), "files", "")
    assert exts == [".txt", ".md"]


def test_resolve_root_files_lowercases_allowlist(srv, tree):
    bridge = _bridge_for(tree, exts=[".TXT", ".Md"])
    _, _, exts = srv._resolve_search_root(bridge, "files", "")
    assert exts == [".txt", ".md"]


def test_resolve_root_narrows_to_sub_path(srv, tree):
    base, start, _ = srv._resolve_search_root(_bridge_for(tree), "app_source", "core")
    assert base == tree.resolve() and start == (tree / "core").resolve()


def test_resolve_root_rejects_unknown_root(srv, tree):
    with pytest.raises(ValueError, match="Unknown root"):
        srv._resolve_search_root(_bridge_for(tree), "everything", "")


def test_resolve_root_rejects_absolute_sub_path(srv, tree):
    with pytest.raises(ValueError, match="must be relative"):
        srv._resolve_search_root(_bridge_for(tree), "app_source", str(tree))


def test_resolve_root_rejects_traversal(srv, tree):
    with pytest.raises(ValueError, match="outside"):
        srv._resolve_search_root(_bridge_for(tree), "app_source", "../..")


def test_resolve_root_rejects_file_sub_path(srv, tree):
    with pytest.raises(ValueError, match="not a directory"):
        srv._resolve_search_root(_bridge_for(tree), "app_source", "notes.md")


def test_resolve_root_rejects_missing_base(srv, tmp_path):
    bridge = make_bridge({"get_app_source_root": {"root": str(tmp_path / "gone")}})
    with pytest.raises(ValueError, match="does not exist"):
        srv._resolve_search_root(bridge, "app_source", "")


def test_resolve_root_files_requires_configured_base_dir(srv):
    bridge = make_bridge({"get_file_io_config": {"base_dir": None}})
    with pytest.raises(ValueError, match="not configured"):
        srv._resolve_search_root(bridge, "files", "")


# ---------------------------------------------------------------------------
# File iteration
# ---------------------------------------------------------------------------


def test_iter_search_files_skips_cache_dirs(srv, tree):
    names = [p.name for p in srv._iter_search_files(tree, "*.py", None)]
    assert "cached.py" not in names
    assert {"molecule.py", "plugin_interface.py"} <= set(names)


def test_iter_search_files_filters_unknown_suffixes(srv, tree):
    names = [p.name for p in srv._iter_search_files(tree, "*", None)]
    assert "data.bin" not in names
    assert "notes.md" in names


def test_iter_search_files_honours_extension_allowlist(srv, tree):
    names = [p.name for p in srv._iter_search_files(tree, "*", [".md"])]
    assert names == ["notes.md"]


def test_iter_search_files_stops_at_file_cap(srv, tree, monkeypatch):
    monkeypatch.setattr(srv, "_GREP_MAX_FILES", 1)
    assert len(list(srv._iter_search_files(tree, "*", None))) == 1


# ---------------------------------------------------------------------------
# run_grep
# ---------------------------------------------------------------------------


def test_grep_finds_definition_with_path_and_line(srv, tree):
    out = srv.run_grep(tree, tree, "def add_menu_action")
    assert "plugins/plugin_interface.py:1: def add_menu_action" in out
    assert "1 match(es) in 1 file(s)" in out


def test_grep_requires_pattern(srv, tree):
    with pytest.raises(ValueError, match="required"):
        srv.run_grep(tree, tree, "")


def test_grep_reports_no_matches(srv, tree):
    out = srv.run_grep(tree, tree, "definitely_not_here")
    assert "No matches" in out and "ignore_case=true" in out


def test_grep_ignore_case(srv, tree):
    assert "No matches" in srv.run_grep(tree, tree, "Class Molecule")
    out = srv.run_grep(tree, tree, "Class Molecule", ignore_case=True)
    assert "core/molecule.py:1:" in out


def test_grep_regex_is_honoured(srv, tree):
    out = srv.run_grep(tree, tree, r"^class \w+:")
    assert "core/molecule.py:1: class Molecule:" in out


def test_grep_fixed_string_escapes_metacharacters(srv, tree):
    (tree / "core" / "regex.py").write_text("value = a.b(c)\n", encoding="utf-8")
    out = srv.run_grep(tree, tree, "a.b(c)", fixed_string=True)
    assert "core/regex.py:1:" in out


def test_grep_invalid_regex_is_explained(srv, tree):
    with pytest.raises(ValueError, match="fixed_string=true"):
        srv.run_grep(tree, tree, "unbalanced(")


def test_grep_glob_filters_files(srv, tree):
    out = srv.run_grep(tree, tree, "add_menu_action", name_glob="*.md")
    assert "notes.md" in out and "plugin_interface.py" not in out


def test_grep_context_lines_use_dash_separator(srv, tree):
    out = srv.run_grep(tree, tree, "'''Register", context=1)
    assert "plugins/plugin_interface.py-1- def add_menu_action" in out
    assert "plugins/plugin_interface.py:2: " in out


def test_grep_context_is_clamped(srv, tree):
    out = srv.run_grep(tree, tree, "def add_menu_action", context=999)
    assert "plugin_interface.py" in out


def test_grep_truncates_at_max_matches(srv, tree):
    out = srv.run_grep(tree, tree, "add_menu_action", name_glob="*", max_matches=1)
    assert "truncated" in out
    assert out.count("\n") == 1  # header + one match line


def test_grep_skips_binary_files(srv, tree):
    out = srv.run_grep(tree, tree, "add_menu_action", name_glob="*")
    assert "data.bin" not in out


def test_grep_skips_oversized_files(srv, tree, monkeypatch):
    monkeypatch.setattr(srv, "_GREP_MAX_FILE_BYTES", 5)
    assert "No matches" in srv.run_grep(tree, tree, "add_menu_action", name_glob="*")


def test_grep_truncates_long_lines(srv, tree):
    (tree / "core" / "long.py").write_text("x = '" + "A" * 900 + "'\n", "utf-8")
    out = srv.run_grep(tree, tree, "AAAA")
    line = next(ln for ln in out.splitlines() if "long.py" in ln)
    assert line.endswith("…")
    assert len(line) < 400


def test_grep_paths_are_relative_to_base_not_start(srv, tree):
    out = srv.run_grep(tree / "core", tree, "class Molecule")
    assert "core/molecule.py:1:" in out


def test_grep_survives_unreadable_file(srv, tree, monkeypatch):
    def _boom(self, *a, **kw):
        raise OSError("locked")

    monkeypatch.setattr(Path, "read_text", _boom)
    assert "No matches" in srv.run_grep(tree, tree, "add_menu_action")


# ---------------------------------------------------------------------------
# run_find
# ---------------------------------------------------------------------------


def test_find_lists_matching_files(srv, tree):
    out = srv.run_find(tree, tree, "*.py")
    assert "core/molecule.py" in out and "plugins/plugin_interface.py" in out
    assert "bytes" in out


def test_find_skips_cache_dirs(srv, tree):
    assert "cached.py" not in srv.run_find(tree, tree, "*.py")


def test_find_reports_nothing_found(srv, tree):
    assert "No files matching" in srv.run_find(tree, tree, "*.nope")


def test_find_truncates_at_max_results(srv, tree):
    out = srv.run_find(tree, tree, "*.py", max_results=1)
    assert "truncated" in out
    assert out.count("\n") == 1


def test_find_clamps_max_results(srv, tree):
    assert "molecule.py" in srv.run_find(tree, tree, "*.py", max_results=99_999)


# ---------------------------------------------------------------------------
# _slice_lines
# ---------------------------------------------------------------------------


def test_slice_lines_without_bounds_returns_text(srv):
    assert srv._slice_lines("a\nb\n", None, None) == "a\nb\n"


def test_slice_lines_returns_range_with_header(srv):
    out = srv._slice_lines("a\nb\nc\nd\n", 2, 3)
    assert out == "[lines 2-3 of 4]\nb\nc"


def test_slice_lines_open_ended(srv):
    assert srv._slice_lines("a\nb\nc\n", 2, None) == "[lines 2-3 of 3]\nb\nc"


def test_slice_lines_clamps_end_beyond_eof(srv):
    assert srv._slice_lines("a\nb\n", 1, 99) == "[lines 1-2 of 2]\na\nb"


def test_slice_lines_start_past_eof_raises(srv):
    with pytest.raises(ValueError, match="past the end"):
        srv._slice_lines("a\nb\n", 5, None)


def test_slice_lines_reversed_range_raises(srv):
    with pytest.raises(ValueError, match="greater than or equal"):
        srv._slice_lines("a\nb\nc\n", 3, 2)


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def test_dispatch_grep_files_defaults_to_app_source(srv, tree):
    result = srv.dispatch_tool(
        _bridge_for(tree), "grep_files", {"pattern": "def add_menu_action"}
    )
    assert "isError" not in result
    assert "plugins/plugin_interface.py:1:" in result["content"][0]["text"]


def test_dispatch_grep_files_with_root_and_sub_path(srv, tree):
    result = srv.dispatch_tool(
        _bridge_for(tree),
        "grep_files",
        {"pattern": "class", "root": "app_source", "path": "core"},
    )
    assert "core/molecule.py:1:" in result["content"][0]["text"]


def test_dispatch_grep_files_root_files_uses_sandbox_allowlist(srv, tree):
    result = srv.dispatch_tool(
        _bridge_for(tree),
        "grep_files",
        {"pattern": "add_menu_action", "root": "files", "glob": "*"},
    )
    text = result["content"][0]["text"]
    assert "notes.md" in text and "plugin_interface.py" not in text


def test_dispatch_grep_files_reports_bad_root(srv, tree):
    result = srv.dispatch_tool(
        _bridge_for(tree), "grep_files", {"pattern": "x", "root": "nope"}
    )
    assert result["isError"] is True
    assert "Unknown root" in result["content"][0]["text"]


def test_dispatch_grep_files_reports_bad_regex(srv, tree):
    result = srv.dispatch_tool(
        _bridge_for(tree), "grep_files", {"pattern": "("}
    )
    assert result["isError"] is True


def test_dispatch_find_files(srv, tree):
    result = srv.dispatch_tool(
        _bridge_for(tree), "find_files", {"pattern": "*interface*.py"}
    )
    text = result["content"][0]["text"]
    assert "plugins/plugin_interface.py" in text and "molecule.py" not in text


def test_dispatch_find_files_defaults_to_every_file(srv, tree):
    result = srv.dispatch_tool(_bridge_for(tree), "find_files", {})
    assert "notes.md" in result["content"][0]["text"]


def test_dispatch_read_text_file_line_range(srv, tmp_path):
    (tmp_path / "log.txt").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    bridge = make_bridge(
        {"get_file_io_config": {"base_dir": str(tmp_path), "allowed_extensions": [".txt"]}}
    )
    result = srv.dispatch_tool(
        bridge, "read_text_file", {"path": "log.txt", "start_line": 2, "end_line": 3}
    )
    assert result["content"][0]["text"] == "[lines 2-3 of 4]\nl2\nl3"


def test_dispatch_read_text_file_without_range_is_unchanged(srv, tmp_path):
    (tmp_path / "log.txt").write_text("l1\nl2\n", encoding="utf-8")
    bridge = make_bridge(
        {"get_file_io_config": {"base_dir": str(tmp_path), "allowed_extensions": [".txt"]}}
    )
    result = srv.dispatch_tool(bridge, "read_text_file", {"path": "log.txt"})
    assert result["content"][0]["text"] == "l1\nl2\n"


def test_dispatch_get_app_source_line_range(srv):
    bridge = make_bridge(
        {"get_app_source": {"type": "file", "content": "a\nb\nc\nd\n"}}
    )
    result = srv.dispatch_tool(
        bridge, "get_app_source", {"path": "x.py", "start_line": 2, "end_line": 3}
    )
    assert result["content"][0]["text"] == "[lines 2-3 of 4]\nb\nc"


def test_dispatch_get_app_source_directory_ignores_line_range(srv):
    bridge = make_bridge(
        {"get_app_source": {"type": "directory", "content": "listing\nrow"}}
    )
    result = srv.dispatch_tool(
        bridge, "get_app_source", {"path": ".", "start_line": 2}
    )
    assert result["content"][0]["text"] == "listing\nrow"


# ---------------------------------------------------------------------------
# Tool schemas and annotations
# ---------------------------------------------------------------------------


def test_search_tools_are_registered(srv):
    names = {t["name"] for t in srv._TOOLS}
    assert {"grep_files", "find_files"} <= names


def test_grep_tool_schema(srv):
    tool = next(t for t in srv._TOOLS if t["name"] == "grep_files")
    props = tool["inputSchema"]["properties"]
    assert tool["inputSchema"]["required"] == ["pattern"]
    assert props["root"]["enum"] == ["app_source", "plugins", "files"]
    assert {"glob", "ignore_case", "fixed_string", "context", "max_matches"} <= set(props)


def test_read_tools_expose_line_range(srv):
    for name in ("read_text_file", "get_app_source"):
        props = next(t for t in srv._TOOLS if t["name"] == name)["inputSchema"]["properties"]
        assert {"start_line", "end_line"} <= set(props)


def test_every_tool_has_annotations(srv):
    for tool in srv._TOOLS:
        assert "readOnlyHint" in tool["annotations"], tool["name"]
        assert "openWorldHint" in tool["annotations"], tool["name"]


def test_read_only_tools_are_marked(srv):
    for name in ("get_current_molecule", "grep_files", "find_files", "read_text_file"):
        tool = next(t for t in srv._TOOLS if t["name"] == name)
        assert tool["annotations"]["readOnlyHint"] is True
        assert "destructiveHint" not in tool["annotations"]


def test_destructive_tools_are_marked(srv):
    for name in ("delete_file", "clear_canvas", "load_molecule_from_smiles"):
        tool = next(t for t in srv._TOOLS if t["name"] == name)
        assert tool["annotations"]["readOnlyHint"] is False
        assert tool["annotations"]["destructiveHint"] is True


def test_idempotent_non_destructive_tools_are_marked(srv):
    tool = next(t for t in srv._TOOLS if t["name"] == "reset_3d_camera")
    assert tool["annotations"]["destructiveHint"] is False
    assert tool["annotations"]["idempotentHint"] is True


def test_network_tools_are_open_world(srv):
    for name in ("load_molecule_by_name", "list_available_plugins"):
        tool = next(t for t in srv._TOOLS if t["name"] == name)
        assert tool["annotations"]["openWorldHint"] is True


def test_local_tools_are_not_open_world(srv):
    tool = next(t for t in srv._TOOLS if t["name"] == "get_bond_info")
    assert tool["annotations"]["openWorldHint"] is False


def test_annotation_name_sets_reference_real_tools(srv):
    names = {t["name"] for t in srv._TOOLS} | {"highlight_bonds"}
    for group in (
        srv._READ_ONLY_TOOLS,
        srv._DESTRUCTIVE_TOOLS,
        srv._IDEMPOTENT_TOOLS,
        srv._OPEN_WORLD_TOOLS,
    ):
        assert group <= names


def test_read_only_and_destructive_sets_are_disjoint(srv):
    assert not (srv._READ_ONLY_TOOLS & srv._DESTRUCTIVE_TOOLS)
    assert not (srv._READ_ONLY_TOOLS & srv._IDEMPOTENT_TOOLS)


def test_grep_stops_scanning_further_files_once_truncated(srv, tree):
    (tree / "core" / "another.py").write_text("class Molecule2:\n", encoding="utf-8")
    out = srv.run_grep(tree, tree, "class Molecule", max_matches=1)
    assert "truncated" in out
    # another.py sorts first, so molecule.py must never be opened.
    assert "core/another.py:1:" in out and "molecule.py" not in out


def test_grep_skips_binary_content_with_text_suffix(srv, tree):
    (tree / "core" / "blob.py").write_bytes(b"class Molecule3:\x00\x01\x02")
    out = srv.run_grep(tree, tree, "class Molecule3")
    assert "No matches" in out


def test_grep_does_not_repeat_overlapping_context_lines(srv, tree):
    (tree / "core" / "pair.py").write_text(
        "hit one\nmiddle\nhit two\n", encoding="utf-8"
    )
    out = srv.run_grep(tree, tree, "^hit", context=1)
    body = [line for line in out.splitlines() if "pair.py" in line]
    assert len(body) == 3  # 3 distinct lines, not 4 with 'middle' twice
    assert sum("middle" in line for line in body) == 1

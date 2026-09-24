"""Tests for the 1.7.0 server tools: schemas, dispatch and the sandboxed
file handling of load_xyz_file, save_molecule_image and compare_structures."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock

import pytest

from conftest import load_module, make_bridge, mock_optional_imports

NEW_TOOLS = (
    "load_xyz_file", "save_molecule_image", "get_3d_camera", "set_3d_camera",
    "measure_geometry", "compare_structures", "clear_overlay",
)

WATER = "3\nwater\nO 0.0 0.0 0.0\nH 0.76 0.59 0.0\nH -0.76 0.59 0.0"
PNG = b"\x89PNG\r\n\x1a\nfake"


@pytest.fixture()
def srv():
    with mock_optional_imports():
        yield load_module("server.py")


def _tool(srv, name):
    return next(t for t in srv._TOOLS if t["name"] == name)


def _text(result):
    return result["content"][0]["text"]


def _sandbox(tmp_path, exts=(".xyz", ".txt")):
    return {"base_dir": str(tmp_path), "allowed_extensions": list(exts)}


def _args_of(bridge, operation):
    return [c.args[1] if len(c.args) > 1 else None for c in bridge.call.call_args_list if c.args[0] == operation]


# ---------------------------------------------------------------------------
# Schemas and annotations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_new_tool_is_listed_with_schema(srv, name):
    tool = _tool(srv, name)
    assert tool["description"]
    assert tool["inputSchema"]["type"] == "object"
    assert "annotations" in tool


@pytest.mark.parametrize("name", ["show_xyz_in_viewer", "load_xyz_file"])
def test_xyz_tools_share_load_options(srv, name):
    props = _tool(srv, name)["inputSchema"]["properties"]
    for key in ("charge", "skip_chemistry", "frame", "keep_camera"):
        assert key in props
    assert props["charge"]["type"] == "integer"


def test_image_tools_accept_atom_labels(srv):
    for name in ("get_molecule_image", "save_molecule_image"):
        assert _tool(srv, name)["inputSchema"]["properties"]["atom_labels"]["type"] == "boolean"


@pytest.mark.parametrize("name,read_only,destructive", [
    ("get_3d_camera", True, None),
    ("measure_geometry", True, None),
    ("load_xyz_file", False, True),
    ("save_molecule_image", False, True),
    ("set_3d_camera", False, False),
    ("clear_overlay", False, False),
    ("compare_structures", False, False),
])
def test_new_tool_annotations(srv, name, read_only, destructive):
    ann = _tool(srv, name)["annotations"]
    assert ann["readOnlyHint"] is read_only
    if destructive is not None:
        assert ann["destructiveHint"] is destructive


def test_required_arguments(srv):
    assert _tool(srv, "load_xyz_file")["inputSchema"]["required"] == ["path"]
    assert _tool(srv, "save_molecule_image")["inputSchema"]["required"] == ["path"]
    assert _tool(srv, "measure_geometry")["inputSchema"]["required"] == ["atoms"]


def test_server_instructions_mention_charge_and_figures(srv):
    assert "charge" in srv._SERVER_INSTRUCTIONS
    assert "save_molecule_image" in srv._SERVER_INSTRUCTIONS


# ---------------------------------------------------------------------------
# show_xyz_in_viewer
# ---------------------------------------------------------------------------


def test_show_xyz_passes_new_options(srv):
    bridge = make_bridge({"show_xyz": {"success": True, "chemistry_skipped": False, "charge": -1,
                                       "num_atoms": 3, "num_bonds": 2}})
    result = srv.dispatch_tool(bridge, "show_xyz_in_viewer", {
        "xyz_text": WATER, "charge": -1, "skip_chemistry": False, "frame": 0, "keep_camera": True,
    })
    sent = _args_of(bridge, "show_xyz")[0]
    assert sent["charge"] == -1 and sent["frame"] == 0 and sent["keep_camera"] is True
    assert "charge -1" in _text(result)


def test_show_xyz_omits_unset_options(srv):
    bridge = make_bridge({"show_xyz": {"success": True}})
    srv.dispatch_tool(bridge, "show_xyz_in_viewer", {"xyz_text": WATER})
    assert set(_args_of(bridge, "show_xyz")[0]) == {"xyz_text", "source_name"}


def test_show_xyz_reports_skipped_chemistry_and_note(srv):
    bridge = make_bridge({"show_xyz": {
        "success": True, "chemistry_skipped": True, "charge": None, "num_atoms": 3, "num_bonds": 2,
        "note": "Bond perception failed with charge 0; loaded with distance-based bonds.",
    }})
    text = _text(srv.dispatch_tool(bridge, "show_xyz_in_viewer", {"xyz_text": WATER}))
    assert "by distance" in text and "Bond perception failed" in text


def test_show_xyz_reports_frames(srv):
    bridge = make_bridge({"show_xyz": {"success": True, "frame": 4, "num_frames": 5}})
    text = _text(srv.dispatch_tool(bridge, "show_xyz_in_viewer", {"xyz_text": WATER}))
    assert "Frame 4 of 5" in text


def test_show_xyz_bridge_error_is_tool_error(srv):
    bridge = MagicMock()
    bridge.call.side_effect = ValueError("'frame' 9 out of range (2 frames)")
    result = srv.dispatch_tool(bridge, "show_xyz_in_viewer", {"xyz_text": WATER, "frame": 9})
    assert result["isError"] is True and "out of range" in _text(result)


# ---------------------------------------------------------------------------
# load_xyz_file
# ---------------------------------------------------------------------------


def test_load_xyz_file_reads_sandbox_and_forwards(srv, tmp_path):
    (tmp_path / "mol").mkdir()
    (tmp_path / "mol" / "water.xyz").write_text(WATER, encoding="utf-8")
    bridge = make_bridge({
        "get_file_io_config": _sandbox(tmp_path),
        "show_xyz": {"success": True, "num_atoms": 3, "num_bonds": 2, "charge": 0,
                     "chemistry_skipped": False},
    })
    result = srv.dispatch_tool(bridge, "load_xyz_file", {"path": "mol/water.xyz", "charge": 0})
    assert result.get("isError") is not True
    sent = _args_of(bridge, "show_xyz")[0]
    assert sent["xyz_text"] == WATER
    assert sent["source_name"] == "mol/water.xyz"
    assert sent["charge"] == 0


def test_load_xyz_file_missing_path_arg(srv):
    result = srv.dispatch_tool(MagicMock(), "load_xyz_file", {})
    assert result["isError"] is True


def test_load_xyz_file_nonexistent(srv, tmp_path):
    bridge = make_bridge({"get_file_io_config": _sandbox(tmp_path)})
    result = srv.dispatch_tool(bridge, "load_xyz_file", {"path": "nope.xyz"})
    assert result["isError"] is True and "does not exist" in _text(result)


def test_load_xyz_file_traversal_refused(srv, tmp_path):
    inner = tmp_path / "box"
    inner.mkdir()
    (tmp_path / "outside.xyz").write_text(WATER, encoding="utf-8")
    bridge = make_bridge({"get_file_io_config": _sandbox(inner)})
    result = srv.dispatch_tool(bridge, "load_xyz_file", {"path": "../outside.xyz"})
    assert result["isError"] is True


def test_load_xyz_file_extension_refused(srv, tmp_path):
    (tmp_path / "water.mol").write_text(WATER, encoding="utf-8")
    bridge = make_bridge({"get_file_io_config": _sandbox(tmp_path, exts=(".xyz",))})
    result = srv.dispatch_tool(bridge, "load_xyz_file", {"path": "water.mol"})
    assert result["isError"] is True


def test_load_xyz_file_size_limit(srv, tmp_path, monkeypatch):
    (tmp_path / "big.xyz").write_text(WATER, encoding="utf-8")
    monkeypatch.setattr(srv, "_MAX_FILE_BYTES", 10)
    bridge = make_bridge({"get_file_io_config": _sandbox(tmp_path)})
    result = srv.dispatch_tool(bridge, "load_xyz_file", {"path": "big.xyz"})
    assert result["isError"] is True and "limit" in _text(result)


def test_load_xyz_file_without_sandbox(srv):
    bridge = make_bridge({"get_file_io_config": {"base_dir": None, "allowed_extensions": []}})
    result = srv.dispatch_tool(bridge, "load_xyz_file", {"path": "water.xyz"})
    assert result["isError"] is True and "not configured" in _text(result)


# ---------------------------------------------------------------------------
# save_molecule_image
# ---------------------------------------------------------------------------


def _image_bridge(tmp_path, view="3d"):
    return make_bridge({
        "get_file_io_config": _sandbox(tmp_path),
        "get_molecule_image": {
            "view": view, "width": 640, "height": 480, "mime_type": "image/png",
            "image_base64": base64.b64encode(PNG).decode("ascii"),
        },
    })


def test_save_image_writes_png_even_if_png_not_in_allowlist(srv, tmp_path):
    bridge = _image_bridge(tmp_path)
    result = srv.dispatch_tool(bridge, "save_molecule_image", {
        "path": "fig/water.png", "view": "3d", "width": 640, "height": 480, "atom_labels": True,
    })
    assert result.get("isError") is not True
    assert (tmp_path / "fig" / "water.png").read_bytes() == PNG
    sent = _args_of(bridge, "get_molecule_image")[0]
    assert sent["atom_labels"] is True and sent["view"] == "3d"
    assert "640x480" in _text(result)


def test_save_image_refuses_non_png(srv, tmp_path):
    result = srv.dispatch_tool(_image_bridge(tmp_path), "save_molecule_image", {"path": "water.txt"})
    assert result["isError"] is True and ".png" in _text(result)
    assert not (tmp_path / "water.txt").exists()


def test_save_image_uppercase_suffix_ok(srv, tmp_path):
    result = srv.dispatch_tool(_image_bridge(tmp_path), "save_molecule_image", {"path": "W.PNG"})
    assert result.get("isError") is not True


def test_save_image_no_overwrite_by_default(srv, tmp_path):
    (tmp_path / "water.png").write_bytes(b"old")
    result = srv.dispatch_tool(_image_bridge(tmp_path), "save_molecule_image", {"path": "water.png"})
    assert result["isError"] is True and "already exists" in _text(result)
    assert (tmp_path / "water.png").read_bytes() == b"old"


def test_save_image_overwrite(srv, tmp_path):
    (tmp_path / "water.png").write_bytes(b"old")
    srv.dispatch_tool(_image_bridge(tmp_path), "save_molecule_image", {"path": "water.png", "overwrite": True})
    assert (tmp_path / "water.png").read_bytes() == PNG


def test_save_image_traversal_refused(srv, tmp_path):
    inner = tmp_path / "box"
    inner.mkdir()
    result = srv.dispatch_tool(_image_bridge(inner), "save_molecule_image", {"path": "../escape.png"})
    assert result["isError"] is True
    assert not (tmp_path / "escape.png").exists()


def test_save_image_missing_path(srv):
    assert srv.dispatch_tool(MagicMock(), "save_molecule_image", {})["isError"] is True


def test_save_image_render_failure_writes_nothing(srv, tmp_path):
    bridge = make_bridge({"get_file_io_config": _sandbox(tmp_path)})
    original = bridge.call.side_effect

    def _call(op, args=None, timeout=10.0):
        if op == "get_molecule_image":
            raise ValueError("Nothing to render: the 3D viewer is empty.")
        return original(op, args, timeout)

    bridge.call.side_effect = _call
    result = srv.dispatch_tool(bridge, "save_molecule_image", {"path": "empty.png"})
    assert result["isError"] is True
    assert not (tmp_path / "empty.png").exists()


def test_get_image_atom_labels_forwarded_only_when_set(srv):
    bridge = make_bridge({"get_molecule_image": {
        "view": "3d", "width": 900, "height": 700, "mime_type": "image/png", "image_base64": "eA==",
    }})
    srv.dispatch_tool(bridge, "get_molecule_image", {"view": "3d", "atom_labels": True})
    srv.dispatch_tool(bridge, "get_molecule_image", {"view": "3d"})
    first, second = _args_of(bridge, "get_molecule_image")
    assert first["atom_labels"] is True
    assert "atom_labels" not in second


# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

CAM = {"position": [0, 0, 10], "focal_point": [0, 0, 0], "view_up": [0, 1, 0]}


def test_get_3d_camera_returns_json(srv):
    result = srv.dispatch_tool(make_bridge({"get_3d_camera": CAM}), "get_3d_camera", {})
    assert json.loads(_text(result)) == CAM


def test_set_3d_camera_forwards_only_given_keys(srv):
    bridge = make_bridge({"set_3d_camera": CAM})
    result = srv.dispatch_tool(bridge, "set_3d_camera", {
        "direction": [1, 0, 0], "view_up": [0, 0, 1], "zoom": 1.4, "position": None,
    })
    assert _args_of(bridge, "set_3d_camera")[0] == {"direction": [1, 0, 0], "view_up": [0, 0, 1], "zoom": 1.4}
    assert _text(result).startswith("Camera set:")


def test_set_3d_camera_error_is_tool_error(srv):
    bridge = MagicMock()
    bridge.call.side_effect = ValueError("'view_up' is parallel to the viewing direction")
    result = srv.dispatch_tool(bridge, "set_3d_camera", {"direction": [0, 1, 0]})
    assert result["isError"] is True and "parallel" in _text(result)


# ---------------------------------------------------------------------------
# measure_geometry
# ---------------------------------------------------------------------------


def test_measure_geometry_formats_lines(srv):
    bridge = make_bridge({"measure_geometry": {"measurements": [
        {"atoms": [0, 1], "symbols": ["O", "H"], "type": "distance", "value": 0.96},
        {"atoms": [1, 0, 2], "symbols": ["H", "O", "H"], "type": "angle", "value": 104.5},
    ], "units": {}}})
    text = _text(srv.dispatch_tool(bridge, "measure_geometry", {"atoms": [[0, 1], [1, 0, 2]]}))
    assert "O0-H1: 0.9600 A" in text
    assert "H1-O0-H2: 104.5000 deg" in text
    assert _args_of(bridge, "measure_geometry")[0] == {"atoms": [[0, 1], [1, 0, 2]]}


# ---------------------------------------------------------------------------
# compare_structures / clear_overlay
# ---------------------------------------------------------------------------

CMP = {
    "rmsd": 0.1234, "atoms_used": 3, "aligned": True,
    "largest_deviations": [{"index": 1, "symbol": "H", "deviation": 0.2}],
}


def test_compare_with_text(srv):
    bridge = make_bridge({"compare_structures": dict(CMP, overlay=True)})
    text = _text(srv.dispatch_tool(bridge, "compare_structures", {
        "xyz_text": WATER, "heavy_atoms_only": True, "overlay": True, "frame": -1,
    }))
    assert "RMSD: 0.1234 A over 3 atoms (aligned)" in text
    assert "H1: 0.2000 A" in text
    assert "Overlay drawn" in text
    sent = _args_of(bridge, "compare_structures")[0]
    assert sent["heavy_atoms_only"] is True and sent["frame"] == -1


def test_compare_with_line_array(srv):
    bridge = make_bridge({"compare_structures": CMP})
    srv.dispatch_tool(bridge, "compare_structures", {"xyz_text": WATER.splitlines()})
    assert _args_of(bridge, "compare_structures")[0]["xyz_text"] == WATER


def test_compare_with_path(srv, tmp_path):
    (tmp_path / "ref.xyz").write_text(WATER, encoding="utf-8")
    bridge = make_bridge({"get_file_io_config": _sandbox(tmp_path), "compare_structures": CMP})
    result = srv.dispatch_tool(bridge, "compare_structures", {"path": "ref.xyz"})
    assert result.get("isError") is not True
    assert _args_of(bridge, "compare_structures")[0]["xyz_text"] == WATER


@pytest.mark.parametrize("args", [{}, {"xyz_text": WATER, "path": "ref.xyz"}])
def test_compare_needs_exactly_one_source(srv, args):
    result = srv.dispatch_tool(MagicMock(), "compare_structures", args)
    assert result["isError"] is True and "exactly one" in _text(result)


def test_clear_overlay(srv):
    text = _text(srv.dispatch_tool(make_bridge({"clear_overlay": {"removed": 2}}), "clear_overlay", {}))
    assert "2 actors" in text

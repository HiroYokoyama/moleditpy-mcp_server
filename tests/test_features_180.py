"""Tests for the 1.8.0 features: user-approved file access (read-only
folders, confirmed sandbox changes), edit_bonds, set_3d_style, batched atom
colors, atom-based camera directions and parallel projection, image
backgrounds, and get_current_molecule on structures without perceived
bonds."""

from __future__ import annotations

import math
import sys
from unittest.mock import MagicMock

import numpy as _real_numpy  # imported before the mocks are installed
import pytest

_NUMPY_MODULES = {
    k: v for k, v in sys.modules.items() if k == "numpy" or k.startswith("numpy.")
}

from conftest import load_module, make_bridge, make_context, mock_optional_imports
from test_bridge_xyz_camera import FakeMol, FakePlotter


@pytest.fixture()
def bridge_mod():
    with mock_optional_imports():
        yield load_module("bridge.py")


@pytest.fixture()
def srv():
    with mock_optional_imports():
        yield load_module("server.py")


@pytest.fixture()
def real_numpy(monkeypatch):
    for name, module in _NUMPY_MODULES.items():
        monkeypatch.setitem(sys.modules, name, module)
    return _real_numpy


def settings_ctx(settings=None):
    """A context whose get/set_setting read and write a real dict."""
    store = dict(settings or {})
    ctx = make_context()
    ctx.get_setting.side_effect = lambda key, default=None: store.get(key, default)
    ctx.set_setting.side_effect = lambda key, value: store.__setitem__(key, value)
    return ctx, store


# ===========================================================================
# Bridge: file access needs the user's approval
# ===========================================================================


def test_file_io_config_lists_only_existing_read_roots(bridge_mod, tmp_path):
    ctx, _ = settings_ctx(
        {"file_io_read_roots": [str(tmp_path), str(tmp_path / "gone"), 3]}
    )
    cfg = bridge_mod.execute_operation(ctx, "get_file_io_config", {})
    assert cfg["read_roots"] == [str(tmp_path)]


def test_file_io_config_read_roots_not_a_list(bridge_mod):
    ctx, _ = settings_ctx({"file_io_read_roots": "oops"})
    assert (
        bridge_mod.execute_operation(ctx, "get_file_io_config", {})["read_roots"] == []
    )


def test_request_read_folder_added_after_yes(bridge_mod, tmp_path, monkeypatch):
    ctx, store = settings_ctx()
    asked = []
    monkeypatch.setattr(
        bridge_mod, "_ask_user", lambda c, title, text: asked.append(text) or True
    )
    result = bridge_mod.execute_operation(
        ctx, "request_read_folder", {"path": str(tmp_path), "reason": "load results"}
    )
    real = str(tmp_path.resolve())
    assert result == {"added": True, "read_roots": [real]}
    assert store["file_io_read_roots"] == [real]
    assert "READ-ONLY" in asked[0] and "load results" in asked[0] and real in asked[0]


def test_request_read_folder_declined_changes_nothing(
    bridge_mod, tmp_path, monkeypatch
):
    ctx, store = settings_ctx()
    monkeypatch.setattr(bridge_mod, "_ask_user", lambda c, t, x: False)
    result = bridge_mod.execute_operation(
        ctx, "request_read_folder", {"path": str(tmp_path)}
    )
    assert result["declined"] is True and result["added"] is False
    assert "file_io_read_roots" not in store


def test_request_read_folder_without_reason_omits_it(bridge_mod, tmp_path, monkeypatch):
    ctx, _ = settings_ctx()
    asked = []
    monkeypatch.setattr(
        bridge_mod, "_ask_user", lambda c, t, x: asked.append(x) or False
    )
    bridge_mod.execute_operation(ctx, "request_read_folder", {"path": str(tmp_path)})
    assert "Reason given" not in asked[0]


@pytest.mark.parametrize("where", ["base", "root"])
def test_request_read_folder_already_readable_asks_nothing(
    bridge_mod, tmp_path, monkeypatch, where
):
    sub = tmp_path / "sub"
    sub.mkdir()
    key = "file_io_base_dir" if where == "base" else "file_io_read_roots"
    value = str(tmp_path) if where == "base" else [str(tmp_path)]
    ctx, _ = settings_ctx({key: value})
    monkeypatch.setattr(bridge_mod, "_ask_user", lambda *a: pytest.fail("must not ask"))
    result = bridge_mod.execute_operation(
        ctx, "request_read_folder", {"path": str(sub)}
    )
    assert result["already_readable"] is True and result["added"] is False


@pytest.mark.parametrize("path", [None, "relative/dir", "/definitely/not/here/xyz"])
def test_request_read_folder_rejects_bad_path(bridge_mod, path):
    ctx, _ = settings_ctx()
    with pytest.raises(ValueError, match="existing absolute directory"):
        bridge_mod.execute_operation(ctx, "request_read_folder", {"path": path})


def test_set_file_io_config_declined_writes_nothing(bridge_mod, tmp_path, monkeypatch):
    ctx, store = settings_ctx()
    asked = []
    monkeypatch.setattr(
        bridge_mod, "_ask_user", lambda c, t, x: asked.append(x) or False
    )
    result = bridge_mod.execute_operation(
        ctx,
        "set_file_io_config",
        {"base_dir": str(tmp_path), "allowed_extensions": [".inp", ".py"]},
    )
    assert result == {"success": False, "declined": True}
    assert store == {}
    assert str(tmp_path) in asked[0] and ".py" in asked[0]


def test_set_file_io_config_unchanged_values_do_not_ask(
    bridge_mod, tmp_path, monkeypatch
):
    ctx, _ = settings_ctx(
        {"file_io_base_dir": str(tmp_path), "file_io_allowed_extensions": [".inp"]}
    )
    monkeypatch.setattr(bridge_mod, "_ask_user", lambda *a: pytest.fail("must not ask"))
    result = bridge_mod.execute_operation(
        ctx,
        "set_file_io_config",
        {"base_dir": str(tmp_path), "allowed_extensions": ["inp"]},
    )
    assert result == {"success": True}


def _qmessagebox(monkeypatch, bridge_mod, answer_is_yes):
    cls = MagicMock()
    cls.StandardButton.Yes = 0x4000  # Qt's values; they combine with |
    cls.StandardButton.No = 0x10000
    box = cls.return_value
    box.exec.return_value = 0x4000 if answer_is_yes else 0x10000
    widgets = MagicMock()
    widgets.QMessageBox = cls
    monkeypatch.setitem(sys.modules, "PyQt6.QtWidgets", widgets)
    timer = MagicMock()
    monkeypatch.setattr(bridge_mod, "QTimer", timer)
    return cls, box, timer


@pytest.mark.parametrize("yes", [True, False])
def test_ask_user_uses_app_dialog_default_no(bridge_mod, monkeypatch, yes):
    cls, box, timer = _qmessagebox(monkeypatch, bridge_mod, yes)
    ctx = make_context()
    assert bridge_mod._ask_user(ctx, "Title", "Text") is yes
    assert cls.call_args.args[0] is ctx.get_main_window.return_value
    box.setStandardButtons.assert_called_once_with(0x4000 | 0x10000)
    box.setDefaultButton.assert_called_once_with(0x10000)
    # the dialog declines by itself before the server stops waiting
    delay, slot = timer.singleShot.call_args.args
    assert slot is box.reject
    assert delay / 1000 < 300.0
    assert bridge_mod._approval_pending is False


def test_ask_user_refuses_a_second_dialog(bridge_mod, monkeypatch):
    _cls, box, _timer = _qmessagebox(monkeypatch, bridge_mod, True)
    inner = []

    def nested_exec():
        # a request served by the dialog's nested event loop
        with pytest.raises(ValueError, match="waiting for the user"):
            bridge_mod._ask_user(make_context(), "Again", "Text")
        inner.append(True)
        return 0x4000

    box.exec.side_effect = nested_exec
    assert bridge_mod._ask_user(make_context(), "Title", "Text") is True
    assert inner == [True]
    assert bridge_mod._approval_pending is False


def test_approval_timeouts_are_ordered(bridge_mod, srv):
    assert bridge_mod.APPROVAL_TIMEOUT_MS / 1000 < srv.APPROVAL_WAIT_SECONDS


# ===========================================================================
# Bridge: 3D style, batched colors
# ===========================================================================


def style_ctx(style="ball_and_stick"):
    ctx = make_context()
    manager = MagicMock()
    manager.current_3d_style = style
    manager._plugin_color_overrides = {}
    ctx.get_main_window.return_value.view_3d_manager = manager
    return ctx, manager


def test_set_3d_style(bridge_mod):
    ctx, manager = style_ctx()
    result = bridge_mod.execute_operation(ctx, "set_3d_style", {"style": "stick"})
    assert result == {"style": "stick", "previous": "ball_and_stick"}
    manager.set_3d_style.assert_called_once_with("stick")


def test_set_3d_style_rejects_unknown(bridge_mod):
    ctx, _ = style_ctx()
    with pytest.raises(ValueError, match="must be one of"):
        bridge_mod.execute_operation(ctx, "set_3d_style", {"style": "cartoon"})


def test_set_3d_style_without_viewer(bridge_mod):
    ctx = make_context()
    ctx.get_main_window.return_value = None
    with pytest.raises(ValueError, match="not available"):
        bridge_mod.execute_operation(ctx, "set_3d_style", {"style": "stick"})


def test_atom_colors_batched_into_one_redraw(bridge_mod):
    ctx, manager = style_ctx()
    ctrl = ctx.get_3d_controller.return_value
    bridge_mod.execute_operation(
        ctx, "highlight_atoms", {"atom_colors": {"0": "#ff0000", "2": "#00ff00"}}
    )
    assert manager._plugin_color_overrides == {0: "#ff0000", 2: "#00ff00"}
    manager.draw_molecule_3d.assert_called_once_with(manager.current_mol)
    ctrl.set_atom_color.assert_not_called()


def test_atom_colors_fall_back_to_controller(bridge_mod):
    ctx = make_context()
    ctx.get_main_window.return_value.view_3d_manager = None
    ctrl = ctx.get_3d_controller.return_value
    bridge_mod.execute_operation(ctx, "highlight_atoms", {"atom_colors": {"1": "#fff"}})
    ctrl.set_atom_color.assert_called_once_with(1, "#fff")
    ctx.refresh_3d_view.assert_called_once()


# ===========================================================================
# Bridge: camera from atoms, parallel projection
# ===========================================================================


def atom_cam_ctx():
    ctx = make_context()
    ctx.plotter = FakePlotter()
    ctx.current_molecule = FakeMol(
        ["C", "C", "C", "H"],
        [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 2]],
    )
    return ctx


def _view_dir(camera):
    pos, focal = camera["position"], camera["focal_point"]
    d = [p - f for p, f in zip(pos, focal)]
    n = math.sqrt(sum(v * v for v in d))
    return [v / n for v in d]


def test_camera_direction_atoms(bridge_mod):
    ctx = atom_cam_ctx()
    cam = bridge_mod.execute_operation(
        ctx, "set_3d_camera", {"direction_atoms": [0, 1], "view_up": [0, 0, 1]}
    )
    assert _view_dir(cam) == pytest.approx([1, 0, 0])
    assert ctx.plotter.reset_calls == 1  # a direction re-frames by default


def test_camera_focal_atoms_centroid(bridge_mod):
    ctx = atom_cam_ctx()
    cam = bridge_mod.execute_operation(ctx, "set_3d_camera", {"focal_atoms": [0, 1]})
    assert cam["focal_point"] == [0.5, 0.0, 0.0]


def test_camera_plane_atoms_faces_the_plane_from_the_current_side(
    bridge_mod, real_numpy
):
    ctx = atom_cam_ctx()  # camera sits on +z
    cam = bridge_mod.execute_operation(ctx, "set_3d_camera", {"plane_atoms": [0, 1, 2]})
    assert _view_dir(cam) == pytest.approx([0, 0, 1], abs=1e-6)
    ctx.plotter.camera_position = [(0.0, 0.0, -10.0), (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    cam = bridge_mod.execute_operation(ctx, "set_3d_camera", {"plane_atoms": [0, 1, 2]})
    assert _view_dir(cam) == pytest.approx([0, 0, -1], abs=1e-6)


def test_camera_plane_atoms_collinear(bridge_mod, real_numpy):
    ctx = atom_cam_ctx()
    ctx.current_molecule = FakeMol(["C"] * 3, [[0, 0, 0], [1, 0, 0], [2, 0, 0]])
    with pytest.raises(ValueError, match="collinear"):
        bridge_mod.execute_operation(ctx, "set_3d_camera", {"plane_atoms": [0, 1, 2]})


@pytest.mark.parametrize(
    "args, msg",
    [
        ({"direction": [1, 0, 0], "direction_atoms": [0, 1]}, "only one"),
        ({"plane_atoms": [0, 1, 2], "position": [1, 2, 3]}, "only one"),
        ({"focal_point": [0, 0, 0], "focal_atoms": [0]}, "not both"),
        ({"direction_atoms": [0, 1, 2]}, "exactly 2"),
        ({"direction_atoms": [0]}, "at least 2"),
        ({"direction_atoms": [0, 9]}, "out of range"),
        ({"plane_atoms": [0, 1]}, "at least 3"),
        ({"focal_atoms": []}, "at least 1"),
        ({"focal_atoms": "0"}, "at least 1"),
    ],
)
def test_camera_atom_arguments_rejected(bridge_mod, args, msg):
    ctx = atom_cam_ctx()
    with pytest.raises(ValueError, match=msg):
        bridge_mod.execute_operation(ctx, "set_3d_camera", args)


def test_camera_direction_atoms_same_position(bridge_mod):
    ctx = atom_cam_ctx()
    ctx.current_molecule = FakeMol(["C", "C"], [[1, 1, 1], [1, 1, 1]])
    with pytest.raises(ValueError, match="same position"):
        bridge_mod.execute_operation(ctx, "set_3d_camera", {"direction_atoms": [0, 1]})


def test_camera_atoms_need_a_3d_structure(bridge_mod):
    ctx = atom_cam_ctx()
    ctx.current_molecule = None
    with pytest.raises(ValueError, match="No 3D structure"):
        bridge_mod.execute_operation(ctx, "set_3d_camera", {"focal_atoms": [0]})


@pytest.mark.parametrize("flag, method", [(True, "enable"), (False, "disable")])
def test_camera_parallel_projection(bridge_mod, flag, method):
    ctx = atom_cam_ctx()
    ctx.plotter.enable_parallel_projection = MagicMock()
    ctx.plotter.disable_parallel_projection = MagicMock()
    ctx.plotter.camera.parallel_projection = flag
    cam = bridge_mod.execute_operation(
        ctx, "set_3d_camera", {"parallel_projection": flag}
    )
    getattr(ctx.plotter, f"{method}_parallel_projection").assert_called_once()
    assert cam["parallel_projection"] is flag


# ===========================================================================
# Bridge: image background
# ===========================================================================


class ShotPlotter:
    def __init__(self):
        self.window_size = [800, 600]
        self.background_color = "gray"
        self.backgrounds = []
        self.kwargs = None
        self.renders = 0

    def set_background(self, color):
        self.backgrounds.append(color)
        self.background_color = color

    def render(self):
        self.renders += 1

    def screenshot(self, path, **kwargs):
        self.kwargs = kwargs
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG fake")


def test_render_3d_background_color_is_restored(bridge_mod):
    ctx = make_context()
    ctx.plotter = ShotPlotter()
    assert bridge_mod._render_3d_png(ctx, 400, 300, "white") == b"\x89PNG fake"
    assert ctx.plotter.backgrounds == ["white", "gray"]
    assert "transparent_background" not in ctx.plotter.kwargs


def test_render_3d_transparent(bridge_mod):
    ctx = make_context()
    ctx.plotter = ShotPlotter()
    bridge_mod._render_3d_png(ctx, 400, 300, "transparent")
    assert ctx.plotter.kwargs["transparent_background"] is True
    assert ctx.plotter.backgrounds == []


def test_render_3d_background_restore_failure_is_tolerated(bridge_mod):
    ctx = make_context()
    plotter = ShotPlotter()
    calls = []

    def flaky(color):
        calls.append(color)
        if len(calls) == 2:
            raise RuntimeError("no")

    plotter.set_background = flaky
    ctx.plotter = plotter
    assert bridge_mod._render_3d_png(ctx, 400, 300, "white") == b"\x89PNG fake"


def test_get_molecule_image_passes_background(bridge_mod, monkeypatch):
    seen = {}

    def fake_render(ctx, width, height, *background):
        seen["bg"] = background
        return b"img"

    monkeypatch.setattr(bridge_mod, "_render_3d_png", fake_render)
    ctx = make_context()
    bridge_mod.execute_operation(
        ctx, "get_molecule_image", {"view": "3d", "background": "transparent"}
    )
    assert seen["bg"] == ("transparent",)


@pytest.mark.parametrize("bad", ["", "   ", 5])
def test_get_molecule_image_rejects_bad_background(bridge_mod, bad):
    with pytest.raises(ValueError, match="background"):
        bridge_mod.execute_operation(
            make_context(), "get_molecule_image", {"view": "3d", "background": bad}
        )


# ===========================================================================
# Bridge with real RDKit: edit_bonds, molecule info fallback
# ===========================================================================


def _real_bridge_module():
    from test_bridge import _real_bridge

    return _real_bridge()


def _three_atoms():
    pytest.importorskip("rdkit")
    from rdkit import Chem
    from rdkit.Chem import AllChem

    mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    AllChem.EmbedMolecule(mol, randomSeed=1)
    return mol


def test_edit_bonds_add_and_remove_keeps_coordinates():
    mod = _real_bridge_module()
    ctx = MagicMock()
    mol = _three_atoms()
    ctx.current_molecule = mol
    n0 = mol.GetNumBonds()
    result = mod._edit_bonds(ctx, {"add": [[0, 2]], "remove": [[1, 2]]})
    assert result["added"] == [[0, 2]] and result["removed"] == [[1, 2]]
    assert result["changed"] is True and result["num_bonds"] == n0
    new = ctx.current_molecule
    assert new.GetBondBetweenAtoms(0, 2) is not None
    assert new.GetBondBetweenAtoms(1, 2) is None
    assert new.GetNumConformers() == 1
    ctx.push_undo_checkpoint.assert_called_once()


def test_edit_bonds_bridging_hydrogen_kept_unsanitized():
    mod = _real_bridge_module()
    ctx = MagicMock()
    mol = _three_atoms()
    ctx.current_molecule = mol
    h = next(
        a.GetIdx()
        for a in mol.GetAtoms()
        if a.GetSymbol() == "H" and a.GetNeighbors()[0].GetIdx() == 0
    )
    result = mod._edit_bonds(ctx, {"add": [[h, 2]]})
    assert result["changed"] is True and result["sanitized"] is False
    assert ctx.current_molecule.GetBondBetweenAtoms(h, 2) is not None


def test_edit_bonds_double_type_and_skips():
    pytest.importorskip("rdkit")
    from rdkit import Chem

    mod = _real_bridge_module()
    ctx = MagicMock()
    ctx.current_molecule = Chem.MolFromSmiles("CC.C")
    result = mod._edit_bonds(
        ctx, {"add": [[1, 2], [0, 1]], "remove": [[0, 2]], "bond_type": "double"}
    )
    assert result["added"] == [[1, 2]]
    reasons = {tuple(s["atoms"]): s["reason"] for s in result["skipped"]}
    assert reasons == {(0, 2): "no such bond", (0, 1): "bond exists"}
    assert (
        ctx.current_molecule.GetBondBetweenAtoms(1, 2).GetBondType()
        == Chem.BondType.DOUBLE
    )


def test_edit_bonds_nothing_to_do_changes_nothing():
    pytest.importorskip("rdkit")
    from rdkit import Chem

    mod = _real_bridge_module()
    ctx = MagicMock()
    mol = Chem.MolFromSmiles("CC")
    ctx.current_molecule = mol
    result = mod._edit_bonds(ctx, {"add": [[0, 1]]})
    assert result["changed"] is False
    assert ctx.current_molecule is mol
    ctx.push_undo_checkpoint.assert_not_called()


@pytest.mark.parametrize(
    "args, msg",
    [
        ({}, "Give 'add'"),
        ({"add": [[0, 0]]}, "itself"),
        ({"add": [[0, 7]]}, "out of range"),
        ({"add": [[0]]}, r"\[i, j\]"),
        ({"add": "0-1"}, "list of"),
        ({"add": [[0, 1]], "bond_type": "quadruple"}, "bond_type"),
    ],
)
def test_edit_bonds_rejects_bad_input(args, msg):
    pytest.importorskip("rdkit")
    from rdkit import Chem

    mod = _real_bridge_module()
    ctx = MagicMock()
    ctx.current_molecule = Chem.MolFromSmiles("CC.C")
    with pytest.raises(ValueError, match=msg):
        mod._edit_bonds(ctx, args)


def test_edit_bonds_needs_a_molecule():
    pytest.importorskip("rdkit")
    mod = _real_bridge_module()
    ctx = MagicMock()
    ctx.current_molecule = None
    with pytest.raises(ValueError, match="No molecule"):
        mod._edit_bonds(ctx, {"add": [[0, 1]]})


def test_molecule_info_without_perceived_bonds(monkeypatch):
    pytest.importorskip("rdkit")
    from rdkit import Chem

    mod = _real_bridge_module()
    ctx = MagicMock()
    ctx.current_molecule = Chem.AddHs(Chem.MolFromSmiles("CCO"))

    def boom(*_a, **_k):
        raise RuntimeError("Pre-condition Violation")

    monkeypatch.setattr(Chem, "MolToSmiles", boom)
    info = mod._get_molecule_info(ctx)
    assert info["smiles"] is None
    assert info["formula"] == "C2H6O"
    assert info["molecular_weight"] == pytest.approx(46.069, abs=0.01)
    assert "not perceived" in info["note"]


@pytest.mark.parametrize(
    "symbols, formula",
    [
        (["C", "H", "H", "O", "C", "H"], "C2H3O"),
        (["O", "H", "H"], "H2O"),
        (["N", "Cl", "B"], "BClN"),
    ],
)
def test_hill_formula(bridge_mod, symbols, formula):
    assert bridge_mod._hill_formula(symbols) == formula


# ===========================================================================
# Server: read paths, approvals, new tools
# ===========================================================================


def cfg_bridge(base, roots=(), extra=None):
    results = {
        "get_file_io_config": {
            "base_dir": str(base) if base else None,
            "allowed_extensions": [".txt", ".xyz", ".png"],
            "read_roots": [str(r) for r in roots],
        }
    }
    results.update(extra or {})
    return make_bridge(results)


def test_read_text_file_absolute_path_inside_read_root(srv, tmp_path):
    base, ro = tmp_path / "base", tmp_path / "ro"
    base.mkdir()
    ro.mkdir()
    (ro / "a.txt").write_text("hello", encoding="utf-8")
    result = srv.dispatch_tool(
        cfg_bridge(base, [ro]), "read_text_file", {"path": str(ro / "a.txt")}
    )
    assert result.get("isError") is not True
    assert "hello" in result["content"][0]["text"]


def test_read_text_file_absolute_path_inside_base(srv, tmp_path):
    (tmp_path / "b.txt").write_text("in base", encoding="utf-8")
    result = srv.dispatch_tool(
        cfg_bridge(tmp_path), "read_text_file", {"path": str(tmp_path / "b.txt")}
    )
    assert "in base" in result["content"][0]["text"]


def test_read_outside_all_roots_is_refused(srv, tmp_path):
    base, other = tmp_path / "base", tmp_path / "other"
    base.mkdir()
    other.mkdir()
    (other / "c.txt").write_text("secret", encoding="utf-8")
    result = srv.dispatch_tool(
        cfg_bridge(base), "read_text_file", {"path": str(other / "c.txt")}
    )
    assert result["isError"] is True
    assert "request_read_folder" in result["content"][0]["text"]


def test_read_root_works_without_base_dir(srv, tmp_path):
    (tmp_path / "d.txt").write_text("x", encoding="utf-8")
    result = srv.dispatch_tool(
        cfg_bridge(None, [tmp_path]),
        "read_text_file",
        {"path": str(tmp_path / "d.txt")},
    )
    assert result.get("isError") is not True


def test_relative_read_without_base_dir_is_refused(srv, tmp_path):
    result = srv.dispatch_tool(
        cfg_bridge(None, [tmp_path]), "read_text_file", {"path": "d.txt"}
    )
    assert (
        result["isError"] is True and "not configured" in result["content"][0]["text"]
    )


def test_list_directory_absolute_read_root(srv, tmp_path):
    (tmp_path / "e.xyz").write_text("1\n\nH 0 0 0\n", encoding="utf-8")
    base = tmp_path / "base"
    base.mkdir()
    result = srv.dispatch_tool(
        cfg_bridge(base, [tmp_path]), "list_directory", {"path": str(tmp_path)}
    )
    assert "e.xyz" in result["content"][0]["text"]


def test_load_xyz_file_from_read_root(srv, tmp_path):
    xyz = tmp_path / "m.xyz"
    xyz.write_text("1\n\nH 0 0 0\n", encoding="utf-8")
    bridge = cfg_bridge(tmp_path / "nope", [tmp_path], {"show_xyz": {"success": True}})
    result = srv.dispatch_tool(bridge, "load_xyz_file", {"path": str(xyz)})
    assert result.get("isError") is not True


def test_writing_into_a_read_root_is_still_refused(srv, tmp_path):
    base, ro = tmp_path / "base", tmp_path / "ro"
    base.mkdir()
    ro.mkdir()
    result = srv.dispatch_tool(
        cfg_bridge(base, [ro]),
        "write_text_file",
        {"path": str(ro / "w.txt"), "content": "x"},
    )
    assert result["isError"] is True
    assert not (ro / "w.txt").exists()


def test_request_read_folder_tool_outcomes(srv, tmp_path):
    for outcome, expect_error, needle in (
        ({"added": True, "read_roots": ["/r"]}, False, "granted"),
        ({"added": False, "declined": True, "read_roots": []}, True, "declined"),
        (
            {"added": False, "already_readable": True, "read_roots": []},
            False,
            "already",
        ),
    ):
        bridge = make_bridge({"request_read_folder": outcome})
        result = srv.dispatch_tool(
            bridge, "request_read_folder", {"path": str(tmp_path), "reason": "why"}
        )
        assert bool(result.get("isError")) is expect_error
        assert needle in result["content"][0]["text"]
        assert bridge.call.call_args.kwargs["timeout"] == srv.APPROVAL_WAIT_SECONDS
        assert bridge.call.call_args.args[1] == {"path": str(tmp_path), "reason": "why"}


def test_request_read_folder_needs_path(srv):
    result = srv.dispatch_tool(make_bridge({}), "request_read_folder", {})
    assert result["isError"] is True


def test_set_file_io_config_declined_is_an_error(srv, tmp_path):
    bridge = make_bridge({"set_file_io_config": {"success": False, "declined": True}})
    result = srv.dispatch_tool(
        bridge, "set_file_io_config", {"base_dir": str(tmp_path)}
    )
    assert result["isError"] is True and "declined" in result["content"][0]["text"]


def test_get_file_io_config_shows_read_roots(srv):
    bridge = cfg_bridge("/base", ["/ro/one", "/ro/two"])
    text = srv.dispatch_tool(bridge, "get_file_io_config", {})["content"][0]["text"]
    assert "Read-only folders:" in text and "/ro/one" in text and "/ro/two" in text
    bridge = cfg_bridge("/base")
    assert (
        "(none)"
        in srv.dispatch_tool(bridge, "get_file_io_config", {})["content"][0]["text"]
    )


def test_get_current_molecule_without_smiles(srv):
    bridge = make_bridge(
        {
            "get_molecule_info": {
                "loaded": True,
                "smiles": None,
                "formula": "C2H6O",
                "molecular_weight": 46.07,
                "num_atoms": 9,
                "num_bonds": 8,
                "has_3d_coords": True,
                "note": "Bond orders are not perceived",
            }
        }
    )
    text = srv.dispatch_tool(bridge, "get_current_molecule", {})["content"][0]["text"]
    assert "SMILES: (not available)" in text and "Note: Bond orders" in text


def test_set_3d_style_tool(srv):
    bridge = make_bridge({"set_3d_style": {"style": "stick", "previous": "cpk"}})
    text = srv.dispatch_tool(bridge, "set_3d_style", {"style": "stick"})["content"][0][
        "text"
    ]
    assert "stick" in text and "cpk" in text


def test_edit_bonds_tool_reports(srv):
    bridge = make_bridge(
        {
            "edit_bonds": {
                "added": [[0, 5]],
                "removed": [[1, 2]],
                "skipped": [{"atoms": [3, 4], "reason": "bond exists"}],
                "changed": True,
                "sanitized": False,
                "num_bonds": 7,
            }
        }
    )
    text = srv.dispatch_tool(
        bridge, "edit_bonds", {"add": [[0, 5], [3, 4]], "remove": [[1, 2]]}
    )["content"][0]["text"]
    for needle in (
        "Added: 0-5",
        "Removed: 1-2",
        "3-4 (bond exists)",
        "7 bonds",
        "Not sanitized",
    ):
        assert needle in text


def test_edit_bonds_tool_no_change(srv):
    bridge = make_bridge(
        {
            "edit_bonds": {
                "added": [],
                "removed": [],
                "skipped": [{"atoms": [0, 1], "reason": "bond exists"}],
                "changed": False,
            }
        }
    )
    text = srv.dispatch_tool(bridge, "edit_bonds", {"add": [[0, 1]]})["content"][0][
        "text"
    ]
    assert text.startswith("No change") and "0-1 (bond exists)" in text


def test_set_3d_camera_passes_atom_keys(srv):
    bridge = make_bridge({"set_3d_camera": {"position": [0, 0, 1]}})
    srv.dispatch_tool(
        bridge,
        "set_3d_camera",
        {"plane_atoms": [0, 1, 2], "focal_atoms": [0], "parallel_projection": False},
    )
    assert bridge.call.call_args.args[1] == {
        "plane_atoms": [0, 1, 2],
        "focal_atoms": [0],
        "parallel_projection": False,
    }


def _image_result():
    return {
        "view": "3d",
        "width": 10,
        "height": 10,
        "mime_type": "image/png",
        "image_base64": "iVBO",
    }


def test_server_image_tools_pass_background(srv):
    bridge = make_bridge({"get_molecule_image": _image_result()})
    srv.dispatch_tool(bridge, "get_molecule_image", {"background": "transparent"})
    assert bridge.call.call_args.args[1]["background"] == "transparent"
    srv.dispatch_tool(bridge, "get_molecule_image", {})
    assert "background" not in bridge.call.call_args.args[1]


def test_save_molecule_image_passes_background(srv, tmp_path):
    bridge = cfg_bridge(tmp_path, extra={"get_molecule_image": _image_result()})
    result = srv.dispatch_tool(
        bridge, "save_molecule_image", {"path": "fig.png", "background": "white"}
    )
    assert result.get("isError") is not True
    assert bridge.call.call_args.args[1]["background"] == "white"


def test_new_tools_are_listed_with_annotations(srv):
    tools = {t["name"]: t for t in srv._TOOLS}
    for name in ("set_3d_style", "edit_bonds", "request_read_folder"):
        assert name in tools
    assert tools["edit_bonds"]["annotations"]["destructiveHint"] is True
    assert tools["set_3d_style"]["annotations"]["idempotentHint"] is True
    assert "background" in tools["save_molecule_image"]["inputSchema"]["properties"]
    assert "plane_atoms" in tools["set_3d_camera"]["inputSchema"]["properties"]
    assert "request_read_folder" in srv._SERVER_INSTRUCTIONS

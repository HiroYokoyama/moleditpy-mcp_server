"""Tests for the 1.7.0 bridge operations: XYZ loading without the charge
dialog, trajectory frames, 3D camera, atom labels, geometry measurement
and structure comparison."""

from __future__ import annotations

import math
import sys
from unittest.mock import MagicMock

import numpy as _real_numpy  # imported before the mocks are installed
import pytest

# numpy and every numpy.* submodule loaded so far, to put back over the mocks
_NUMPY_MODULES = {
    k: v for k, v in sys.modules.items() if k == "numpy" or k.startswith("numpy.")
}

from conftest import load_module, make_context, mock_optional_imports


@pytest.fixture()
def bridge_mod():
    with mock_optional_imports():
        yield load_module("bridge.py")


@pytest.fixture()
def real_numpy(monkeypatch):
    """Put the real numpy back for tests that check the math."""
    for name, module in _NUMPY_MODULES.items():
        monkeypatch.setitem(sys.modules, name, module)
    return _real_numpy


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAtom:
    def __init__(self, symbol: str) -> None:
        self._symbol = symbol

    def GetSymbol(self) -> str:
        return self._symbol


class FakeBond:
    def __init__(self, a: int, b: int) -> None:
        self._a, self._b = a, b

    def GetBeginAtomIdx(self) -> int:
        return self._a

    def GetEndAtomIdx(self) -> int:
        return self._b


class FakeConf:
    def __init__(self, coords: list[list[float]]) -> None:
        self._coords = coords

    def GetAtomPosition(self, i: int) -> list[float]:
        return list(self._coords[i])


class FakeMol:
    """Just enough of an RDKit Mol for the geometry tools."""

    def __init__(self, symbols, coords, bonds=(), props=None) -> None:
        self._symbols = list(symbols)
        self._conf = FakeConf([list(c) for c in coords]) if coords is not None else None
        self._bonds = [FakeBond(a, b) for a, b in bonds]
        self._props = dict(props or {})

    def GetNumAtoms(self) -> int:
        return len(self._symbols)

    def GetNumBonds(self) -> int:
        return len(self._bonds)

    def GetBonds(self):
        return list(self._bonds)

    def GetNumConformers(self) -> int:
        return 0 if self._conf is None else 1

    def GetConformer(self) -> FakeConf:
        return self._conf

    def GetAtomWithIdx(self, i: int) -> FakeAtom:
        return FakeAtom(self._symbols[i])

    def HasProp(self, key: str) -> bool:
        return key in self._props

    def GetIntProp(self, key: str) -> int:
        return self._props[key]


# Water: O at origin, H-O-H 104.5 deg, O-H 0.96 A
_HALF = math.radians(104.5 / 2)
WATER_SYMBOLS = ["O", "H", "H"]
WATER_COORDS = [
    [0.0, 0.0, 0.0],
    [0.96 * math.sin(_HALF), 0.96 * math.cos(_HALF), 0.0],
    [-0.96 * math.sin(_HALF), 0.96 * math.cos(_HALF), 0.0],
]


def water_xyz(coords=WATER_COORDS, comment="water") -> str:
    rows = [
        f"{s} {x:.6f} {y:.6f} {z:.6f}" for s, (x, y, z) in zip(WATER_SYMBOLS, coords)
    ]
    return "\n".join([str(len(rows)), comment] + rows)


class FakeIOManager:
    """Mimics the app's XYZ loader: tries charge 0 unless 'always ask',
    then asks prompt_for_charge until perception succeeds or is skipped."""

    def __init__(self, settings: dict, fails_for=()) -> None:
        self.settings = settings
        self.fails_for = set(fails_for)
        self.dialog_opened = 0
        self.calls: list[tuple] = []

    def prompt_for_charge(self):  # the real one opens a modal dialog
        self.dialog_opened += 1
        raise AssertionError("modal charge dialog opened")

    def load(self, text: str) -> FakeMol | None:
        self.calls.append(("load", text))
        if self.settings.get("skip_chemistry_checks"):
            return FakeMol(
                ["O", "H", "H"],
                WATER_COORDS,
                [(0, 1)],
                {"_xyz_skip_checks": 1, "_xyz_charge": 0},
            )
        if not self.settings.get("always_ask_charge") and 0 not in self.fails_for:
            return FakeMol(
                ["O", "H", "H"], WATER_COORDS, [(0, 1), (0, 2)], {"_xyz_charge": 0}
            )
        while True:
            charge, ok, skip = self.prompt_for_charge()
            if not ok:
                return None
            if skip:
                return FakeMol(
                    ["O", "H", "H"],
                    WATER_COORDS,
                    [(0, 1)],
                    {"_xyz_skip_checks": 1, "_xyz_charge": 0},
                )
            if charge not in self.fails_for:
                return FakeMol(
                    ["O", "H", "H"],
                    WATER_COORDS,
                    [(0, 1), (0, 2)],
                    {"_xyz_charge": charge},
                )


def xyz_ctx(settings=None, fails_for=()):
    ctx = make_context()
    settings = {} if settings is None else settings
    io_mgr = FakeIOManager(settings, fails_for)
    mw = MagicMock()
    mw.io_manager = io_mgr
    mw.init_manager.settings = settings
    ctx.get_main_window.return_value = mw
    ctx.show_xyz_data.side_effect = lambda text, source_name="": io_mgr.load(text)
    return ctx, io_mgr, settings


# ---------------------------------------------------------------------------
# show_xyz: charge dialog handling
# ---------------------------------------------------------------------------


def test_show_xyz_default_charge_zero_no_dialog(bridge_mod):
    ctx, io_mgr, _ = xyz_ctx()
    result = bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": water_xyz()})
    assert result["success"] is True
    assert result["charge"] == 0
    assert result["chemistry_skipped"] is False
    assert io_mgr.dialog_opened == 0


def test_show_xyz_explicit_charge_answers_prompt(bridge_mod):
    ctx, io_mgr, settings = xyz_ctx()
    result = bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": water_xyz(), "charge": -1}
    )
    assert result["charge"] == -1
    assert result["chemistry_skipped"] is False
    assert io_mgr.dialog_opened == 0
    assert "note" not in result


def test_show_xyz_explicit_charge_bypasses_silent_zero_attempt(bridge_mod):
    """Charge 0 would 'succeed' here, but the caller asked for +1."""
    ctx, _, _ = xyz_ctx()
    result = bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": water_xyz(), "charge": 1}
    )
    assert result["charge"] == 1


def test_show_xyz_charge_zero_fails_falls_back_instead_of_dialog(bridge_mod):
    ctx, io_mgr, _ = xyz_ctx(fails_for={0})
    result = bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": water_xyz()})
    assert result["success"] is True
    assert result["chemistry_skipped"] is True
    assert result["charge"] is None
    assert "Pass 'charge'" in result["note"]
    assert io_mgr.dialog_opened == 0


def test_show_xyz_wrong_explicit_charge_falls_back_once(bridge_mod):
    ctx, _, _ = xyz_ctx(fails_for={2})
    result = bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": water_xyz(), "charge": 2}
    )
    assert result["chemistry_skipped"] is True
    assert "charge 2" in result["note"]


def test_show_xyz_skip_chemistry(bridge_mod):
    ctx, _, _ = xyz_ctx()
    result = bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": water_xyz(), "skip_chemistry": True}
    )
    assert result["chemistry_skipped"] is True
    assert result["num_bonds"] == 1


def test_show_xyz_restores_settings_and_prompt(bridge_mod):
    settings = {"skip_chemistry_checks": True}
    ctx, io_mgr, _ = xyz_ctx(settings)
    bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": water_xyz(), "charge": -1}
    )
    assert settings == {"skip_chemistry_checks": True}
    assert "prompt_for_charge" not in vars(io_mgr)
    with pytest.raises(AssertionError):
        io_mgr.prompt_for_charge()


def test_show_xyz_restores_instance_prompt_override(bridge_mod):
    ctx, io_mgr, _ = xyz_ctx()
    custom = MagicMock(return_value=(0, True, False))
    io_mgr.prompt_for_charge = custom
    bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": water_xyz(), "charge": 3}
    )
    assert io_mgr.prompt_for_charge is custom


def test_show_xyz_restores_settings_when_load_raises(bridge_mod):
    settings = {"always_ask_charge": False}
    ctx, _, _ = xyz_ctx(settings)
    ctx.show_xyz_data.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        bridge_mod.execute_operation(
            ctx, "show_xyz", {"xyz_text": water_xyz(), "skip_chemistry": True}
        )
    assert settings == {"always_ask_charge": False}


@pytest.mark.parametrize("bad", ["1", 1.5, True])
def test_show_xyz_rejects_non_integer_charge(bridge_mod, bad):
    ctx, _, _ = xyz_ctx()
    with pytest.raises(ValueError, match="charge"):
        bridge_mod.execute_operation(
            ctx, "show_xyz", {"xyz_text": water_xyz(), "charge": bad}
        )


def test_show_xyz_without_main_window_still_loads(bridge_mod):
    ctx = make_context()
    ctx.get_main_window.return_value = None
    ctx.show_xyz_data.return_value = FakeMol(
        WATER_SYMBOLS, WATER_COORDS, props={"_xyz_charge": 0}
    )
    result = bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": water_xyz(), "charge": 0}
    )
    assert result["success"] is True


def test_show_xyz_failure_reports_false(bridge_mod):
    ctx, _, _ = xyz_ctx()
    ctx.show_xyz_data.side_effect = lambda text, source_name="": None
    assert bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": water_xyz()}) == {
        "success": False
    }


# ---------------------------------------------------------------------------
# show_xyz: frames and camera
# ---------------------------------------------------------------------------


def _trajectory(n: int = 3) -> str:
    frames = []
    for k in range(n):
        coords = [[x + 0.1 * k, y, z] for x, y, z in WATER_COORDS]
        frames.append(water_xyz(coords, comment=f"step {k}"))
    return "\n".join(frames)


def test_split_xyz_blocks_trajectory(bridge_mod):
    blocks = bridge_mod.split_xyz_blocks(_trajectory(3))
    assert len(blocks) == 3
    assert blocks[2][0] == "step 2"
    assert len(blocks[0][1]) == 3


def test_split_xyz_blocks_headerless(bridge_mod):
    blocks = bridge_mod.split_xyz_blocks("O 0 0 0\nH 0 0 1\n\n")
    assert blocks == [("", ["O 0 0 0", "H 0 0 1"])]


def test_split_xyz_blocks_truncated(bridge_mod):
    with pytest.raises(ValueError, match="truncated"):
        bridge_mod.split_xyz_blocks("3\ncomment\nO 0 0 0\n")


def test_parse_xyz_frames_rows(bridge_mod):
    frames = bridge_mod.parse_xyz_frames("2\n\no 0 0 0\nh 0 0 0.96")
    assert frames == [[("O", 0.0, 0.0, 0.0), ("H", 0.0, 0.0, 0.96)]]


def test_parse_xyz_frames_bad_row(bridge_mod):
    with pytest.raises(ValueError, match="Element X Y Z"):
        bridge_mod.parse_xyz_frames("O 0 0")


def test_show_xyz_trajectory_defaults_to_last_frame(bridge_mod):
    ctx, io_mgr, _ = xyz_ctx()
    result = bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": _trajectory(3)})
    assert result["frame"] == 2 and result["num_frames"] == 3
    loaded = io_mgr.calls[-1][1]
    assert loaded.splitlines()[1] == "step 2"
    assert len(loaded.splitlines()) == 5


@pytest.mark.parametrize("frame,expected", [(0, 0), (1, 1), (-3, 0), (-1, 2)])
def test_show_xyz_trajectory_frame_selection(bridge_mod, frame, expected):
    ctx, io_mgr, _ = xyz_ctx()
    result = bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": _trajectory(3), "frame": frame}
    )
    assert result["frame"] == expected
    assert io_mgr.calls[-1][1].splitlines()[1] == f"step {expected}"


def test_show_xyz_frame_out_of_range(bridge_mod):
    ctx, _, _ = xyz_ctx()
    with pytest.raises(ValueError, match="out of range"):
        bridge_mod.execute_operation(
            ctx, "show_xyz", {"xyz_text": _trajectory(2), "frame": 5}
        )


def test_show_xyz_single_frame_passthrough_untouched(bridge_mod):
    """No frame argument + one frame: the app gets the exact text (its own
    parser handles quirks such as ghost-atom rows)."""
    ctx, io_mgr, _ = xyz_ctx()
    text = "3\nwater\nO 0 0 0\nH 0.76 0.59 0\nXX : 0 0 5"
    result = bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": text})
    assert io_mgr.calls[-1][1] == text
    assert "num_frames" not in result


def test_show_xyz_unparseable_text_without_frame_passes_through(bridge_mod):
    ctx, io_mgr, _ = xyz_ctx()
    text = "3\nshort\nO 0 0 0"
    bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": text})
    assert io_mgr.calls[-1][1] == text


def test_show_xyz_keep_camera_restores_view(bridge_mod):
    ctx, _, _ = xyz_ctx()
    saved = [(1.0, 2.0, 3.0), (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    ctx.plotter.camera_position = saved

    def _load(text, source_name=""):
        ctx.plotter.camera_position = [(9, 9, 9), (0, 0, 0), (0, 0, 1)]  # app re-frames
        return FakeMol(WATER_SYMBOLS, WATER_COORDS, props={"_xyz_charge": 0})

    ctx.show_xyz_data.side_effect = _load
    bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": water_xyz(), "keep_camera": True}
    )
    assert ctx.plotter.camera_position == saved


def test_show_xyz_without_keep_camera_leaves_reframe(bridge_mod):
    ctx, _, _ = xyz_ctx()
    ctx.plotter.camera_position = "before"

    def _load(text, source_name=""):
        ctx.plotter.camera_position = "after"
        return FakeMol(WATER_SYMBOLS, WATER_COORDS, props={"_xyz_charge": 0})

    ctx.show_xyz_data.side_effect = _load
    bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": water_xyz()})
    assert ctx.plotter.camera_position == "after"


def test_show_xyz_resets_camera_after_load(bridge_mod):
    ctx, _, _ = xyz_ctx()
    bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": water_xyz()})
    ctx.reset_3d_camera.assert_called_once_with()


def test_show_xyz_keep_camera_skips_reset(bridge_mod):
    ctx, _, _ = xyz_ctx()
    ctx.plotter.camera_position = [(1, 2, 3), (0, 0, 0), (0, 1, 0)]
    bridge_mod.execute_operation(
        ctx, "show_xyz", {"xyz_text": water_xyz(), "keep_camera": True}
    )
    ctx.reset_3d_camera.assert_not_called()


def test_show_xyz_failed_load_does_not_reset_camera(bridge_mod):
    ctx, _, _ = xyz_ctx()
    ctx.show_xyz_data.side_effect = lambda text, source_name="": None
    bridge_mod.execute_operation(ctx, "show_xyz", {"xyz_text": water_xyz()})
    ctx.reset_3d_camera.assert_not_called()


# ---------------------------------------------------------------------------
# 3D camera
# ---------------------------------------------------------------------------


class FakePlotter:
    def __init__(self) -> None:
        self.camera_position = [(0.0, 0.0, 10.0), (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
        self.camera = MagicMock()
        self.reset_calls = 0
        self.renders = 0
        self.labels = []
        self.removed = []
        self.meshes = []

    def reset_camera(self) -> None:
        self.reset_calls += 1

    def render(self) -> None:
        self.renders += 1

    def add_point_labels(self, points, labels, **kw):
        self.labels.append((points, labels, kw))
        return "label-actor"

    def remove_actor(self, actor):
        self.removed.append(actor)
        return True

    def add_mesh(self, mesh, **kw):
        self.meshes.append((mesh, kw))


def cam_ctx():
    ctx = make_context()
    ctx.plotter = FakePlotter()
    return ctx


def test_get_3d_camera(bridge_mod):
    ctx = cam_ctx()
    assert bridge_mod.execute_operation(ctx, "get_3d_camera", {}) == {
        "position": [0.0, 0.0, 10.0],
        "focal_point": [0.0, 0.0, 0.0],
        "view_up": [0.0, 1.0, 0.0],
    }


def test_get_3d_camera_no_viewer(bridge_mod):
    ctx = make_context()
    ctx.plotter = None
    with pytest.raises(ValueError, match="not available"):
        bridge_mod.execute_operation(ctx, "get_3d_camera", {})


def test_set_3d_camera_direction_keeps_distance_and_fits(bridge_mod):
    ctx = cam_ctx()
    result = bridge_mod.execute_operation(
        ctx, "set_3d_camera", {"direction": [1, 0, 0], "view_up": [0, 0, 1]}
    )
    assert result["position"] == [10.0, 0.0, 0.0]
    assert result["view_up"] == [0.0, 0.0, 1.0]
    assert ctx.plotter.reset_calls == 1


def test_set_3d_camera_direction_is_normalized(bridge_mod):
    ctx = cam_ctx()
    result = bridge_mod.execute_operation(
        ctx,
        "set_3d_camera",
        {"direction": [0, 5, 0], "view_up": [0, 0, 1], "fit": False},
    )
    assert result["position"] == [0.0, 10.0, 0.0]
    assert ctx.plotter.reset_calls == 0


def test_set_3d_camera_kept_view_up_along_direction_is_replaced(bridge_mod):
    ctx = cam_ctx()  # current view_up is +y
    result = bridge_mod.execute_operation(
        ctx, "set_3d_camera", {"direction": [0, 1, 0], "fit": False}
    )
    assert result["position"] == [0.0, 10.0, 0.0]
    up = result["view_up"]
    assert abs(up[1]) < 1e-9 and abs(sum(v * v for v in up) - 1.0) < 1e-9


def test_set_3d_camera_position_does_not_fit_by_default(bridge_mod):
    ctx = cam_ctx()
    result = bridge_mod.execute_operation(
        ctx, "set_3d_camera", {"position": [3, 4, 0], "view_up": [0, 0, 1]}
    )
    assert result["position"] == [3.0, 4.0, 0.0]
    assert ctx.plotter.reset_calls == 0


def test_set_3d_camera_zoom(bridge_mod):
    ctx = cam_ctx()
    bridge_mod.execute_operation(ctx, "set_3d_camera", {"zoom": 1.5})
    ctx.plotter.camera.zoom.assert_called_once_with(1.5)


@pytest.mark.parametrize(
    "args,msg",
    [
        ({"position": [1, 0, 0], "direction": [1, 0, 0]}, "only one"),
        ({"direction": [0, 0, 0]}, "non-zero"),
        ({"direction": [0, 1, 0], "view_up": [0, 1, 0]}, "parallel"),
        ({"position": [0, 0, 0]}, "differ"),
        ({"view_up": [0, 0, 0]}, "non-zero"),
        ({"direction": [1, 0]}, "3 numbers"),
        ({"direction": ["a", 0, 0]}, "3 numbers"),
        ({"direction": [float("nan"), 0, 0]}, "finite"),
        ({"zoom": 0}, "positive"),
        ({"zoom": -2}, "positive"),
    ],
)
def test_set_3d_camera_rejects_bad_input(bridge_mod, args, msg):
    ctx = cam_ctx()
    with pytest.raises(ValueError, match=msg):
        bridge_mod.execute_operation(ctx, "set_3d_camera", args)


def test_set_3d_camera_no_viewer(bridge_mod):
    ctx = make_context()
    ctx.plotter = None
    with pytest.raises(ValueError, match="not available"):
        bridge_mod.execute_operation(ctx, "set_3d_camera", {"zoom": 2})


# ---------------------------------------------------------------------------
# Atom index labels on the 3D capture
# ---------------------------------------------------------------------------


def test_atom_labels_added_and_removed_around_capture(bridge_mod, monkeypatch):
    ctx = cam_ctx()
    ctx.current_molecule = FakeMol(WATER_SYMBOLS, WATER_COORDS)
    seen = {}

    def _fake_render(c, w, h):
        seen["labels_during_capture"] = len(c.plotter.labels) - len(c.plotter.removed)
        return b"\x89PNG"

    monkeypatch.setattr(bridge_mod, "_render_3d_png", _fake_render)
    result = bridge_mod.execute_operation(
        ctx, "get_molecule_image", {"view": "3d", "atom_labels": True}
    )
    assert result["view"] == "3d"
    assert seen["labels_during_capture"] == 1
    _points, labels, _kw = ctx.plotter.labels[0]
    assert labels == ["0", "1", "2"]
    assert ctx.plotter.removed == ["label-actor"]


def test_atom_labels_removed_even_if_capture_fails(bridge_mod, monkeypatch):
    ctx = cam_ctx()
    ctx.current_molecule = FakeMol(WATER_SYMBOLS, WATER_COORDS)

    def _boom(c, w, h):
        raise RuntimeError("render failed")

    monkeypatch.setattr(bridge_mod, "_render_3d_png", _boom)
    with pytest.raises(RuntimeError):
        bridge_mod.execute_operation(
            ctx, "get_molecule_image", {"view": "3d", "atom_labels": True}
        )
    assert ctx.plotter.removed == ["label-actor"]


def test_atom_labels_off_by_default(bridge_mod, monkeypatch):
    ctx = cam_ctx()
    ctx.current_molecule = FakeMol(WATER_SYMBOLS, WATER_COORDS)
    monkeypatch.setattr(bridge_mod, "_render_3d_png", lambda c, w, h: b"png")
    bridge_mod.execute_operation(ctx, "get_molecule_image", {"view": "3d"})
    assert ctx.plotter.labels == []


def test_atom_labels_ignored_for_2d(bridge_mod, monkeypatch):
    ctx = cam_ctx()
    ctx.current_molecule = FakeMol(WATER_SYMBOLS, WATER_COORDS)
    monkeypatch.setattr(bridge_mod, "_render_2d_png", lambda c, w, h: b"png")
    bridge_mod.execute_operation(
        ctx, "get_molecule_image", {"view": "2d", "atom_labels": True}
    )
    assert ctx.plotter.labels == []


# ---------------------------------------------------------------------------
# measure_geometry
# ---------------------------------------------------------------------------


def geo_ctx(symbols=WATER_SYMBOLS, coords=WATER_COORDS):
    ctx = make_context()
    ctx.current_molecule = FakeMol(symbols, coords)
    return ctx


def test_measure_distance_and_angle(bridge_mod):
    result = bridge_mod.execute_operation(
        geo_ctx(), "measure_geometry", {"atoms": [[0, 1], [1, 0, 2]]}
    )
    dist, ang = result["measurements"]
    assert dist["type"] == "distance" and dist["value"] == pytest.approx(0.96, abs=1e-4)
    assert dist["symbols"] == ["O", "H"]
    assert ang["type"] == "angle" and ang["value"] == pytest.approx(104.5, abs=1e-3)


@pytest.mark.parametrize("z,expected", [(1.0, 45.0), (-1.0, -45.0), (0.0, 0.0)])
def test_measure_dihedral_sign(bridge_mod, z, expected):
    # a on +y, b-c along x, d rotated about x
    coords = [[0, 1, 0], [0, 0, 0], [1, 0, 0], [1, 1, z]]
    ctx = geo_ctx(["C"] * 4, coords)
    result = bridge_mod.execute_operation(
        ctx, "measure_geometry", {"atoms": [[0, 1, 2, 3]]}
    )
    assert result["measurements"][0]["value"] == pytest.approx(expected, abs=1e-6)


def test_measure_dihedral_trans_is_180(bridge_mod):
    coords = [[0, 1, 0], [0, 0, 0], [1, 0, 0], [1, -1, 0]]
    result = bridge_mod.execute_operation(
        geo_ctx(["C"] * 4, coords), "measure_geometry", {"atoms": [[0, 1, 2, 3]]}
    )
    assert abs(result["measurements"][0]["value"]) == pytest.approx(180.0)


def test_measure_units_reported(bridge_mod):
    result = bridge_mod.execute_operation(
        geo_ctx(), "measure_geometry", {"atoms": [[0, 1]]}
    )
    assert result["units"]["distance"] == "angstrom"


@pytest.mark.parametrize(
    "atoms,msg",
    [
        ([], "non-empty"),
        (None, "non-empty"),
        ([[0]], "2, 3 or 4"),
        ([[0, 1, 2, 0, 1]], "2, 3 or 4"),
        ([[0, 0]], "Repeated"),
        ([[0, 9]], "out of range|index"),
        ([[0, "x"]], "integer|index"),
    ],
)
def test_measure_rejects_bad_input(bridge_mod, atoms, msg):
    with pytest.raises(ValueError, match=msg):
        bridge_mod.execute_operation(geo_ctx(), "measure_geometry", {"atoms": atoms})


def test_measure_too_many(bridge_mod):
    with pytest.raises(ValueError, match="500"):
        bridge_mod.execute_operation(
            geo_ctx(), "measure_geometry", {"atoms": [[0, 1]] * 501}
        )


def test_measure_collinear_dihedral(bridge_mod):
    coords = [[0, 0, 0], [1, 0, 0], [2, 0, 0], [3, 1, 0]]
    with pytest.raises(ValueError, match="collinear"):
        bridge_mod.execute_operation(
            geo_ctx(["C"] * 4, coords), "measure_geometry", {"atoms": [[0, 1, 2, 3]]}
        )


def test_measure_coincident_angle(bridge_mod):
    coords = [[0, 0, 0], [0, 0, 0], [1, 0, 0]]
    with pytest.raises(ValueError, match="coincide"):
        bridge_mod.execute_operation(
            geo_ctx(["C"] * 3, coords), "measure_geometry", {"atoms": [[0, 1, 2]]}
        )


def test_measure_no_3d(bridge_mod):
    ctx = make_context()
    ctx.current_molecule = FakeMol(WATER_SYMBOLS, None)
    with pytest.raises(ValueError, match="No 3D"):
        bridge_mod.execute_operation(ctx, "measure_geometry", {"atoms": [[0, 1]]})


# ---------------------------------------------------------------------------
# compare_structures
# ---------------------------------------------------------------------------


def _rotate_z(coords, deg, shift=(0.0, 0.0, 0.0)):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [
        [c * x - s * y + shift[0], s * x + c * y + shift[1], z + shift[2]]
        for x, y, z in coords
    ]


def cmp_ctx():
    ctx = cam_ctx()
    ctx.current_molecule = FakeMol(WATER_SYMBOLS, WATER_COORDS, bonds=[(0, 1), (0, 2)])
    return ctx


def test_compare_identical_after_rigid_motion_is_zero(bridge_mod, real_numpy):
    moved = _rotate_z(WATER_COORDS, 73.0, shift=(1.0, -2.0, 0.5))
    result = bridge_mod.execute_operation(
        cmp_ctx(), "compare_structures", {"xyz_text": water_xyz(moved)}
    )
    assert result["rmsd"] == pytest.approx(0.0, abs=1e-4)
    assert result["aligned"] is True
    assert result["atoms_used"] == 3


def test_compare_without_alignment_sees_the_shift(bridge_mod, real_numpy):
    moved = [[x + 1.0, y, z] for x, y, z in WATER_COORDS]
    result = bridge_mod.execute_operation(
        cmp_ctx(), "compare_structures", {"xyz_text": water_xyz(moved), "align": False}
    )
    assert result["rmsd"] == pytest.approx(1.0, abs=1e-4)


def test_compare_reports_largest_deviation_first(bridge_mod, real_numpy):
    stretched = [list(c) for c in WATER_COORDS]
    stretched[1] = [v * 1.2 for v in stretched[1]]
    result = bridge_mod.execute_operation(
        cmp_ctx(),
        "compare_structures",
        {"xyz_text": water_xyz(stretched), "align": False},
    )
    top = result["largest_deviations"][0]
    assert top["index"] == 1 and top["symbol"] == "H"
    assert top["deviation"] == pytest.approx(0.192, abs=1e-3)


def test_compare_no_reflection(bridge_mod, real_numpy):
    """A mirror image must not be 'aligned' to zero by an improper rotation."""
    chiral = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
    mirror = [[x, y, -z] for x, y, z in chiral]
    ctx = cam_ctx()
    ctx.current_molecule = FakeMol(["C", "H", "F", "Cl"], chiral)
    text = "\n".join(
        ["4", ""]
        + [f"{s} {x} {y} {z}" for s, (x, y, z) in zip(["C", "H", "F", "Cl"], mirror)]
    )
    result = bridge_mod.execute_operation(ctx, "compare_structures", {"xyz_text": text})
    assert result["rmsd"] > 0.1


def test_compare_heavy_atoms_only(bridge_mod, real_numpy):
    moved = [list(c) for c in WATER_COORDS]
    moved[1] = [5.0, 5.0, 5.0]  # only an H moved
    result = bridge_mod.execute_operation(
        cmp_ctx(),
        "compare_structures",
        {"xyz_text": water_xyz(moved), "heavy_atoms_only": True},
    )
    assert result["atoms_used"] == 1
    assert result["rmsd"] == pytest.approx(0.0, abs=1e-6)


def test_compare_trajectory_frame(bridge_mod, real_numpy):
    traj = (
        water_xyz(comment="a")
        + "\n"
        + water_xyz([[x + 1, y, z] for x, y, z in WATER_COORDS], "b")
    )
    ctx = cmp_ctx()
    last = bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": traj, "align": False}
    )
    first = bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": traj, "align": False, "frame": 0}
    )
    assert last["rmsd"] == pytest.approx(1.0, abs=1e-4)
    assert first["rmsd"] == pytest.approx(0.0, abs=1e-6)


class FakeView3D:
    """The app's 3D manager: style switch (redraws) and the override store."""

    def __init__(self, style="ball_and_stick", overrides=None) -> None:
        self.current_3d_style = style
        self._plugin_color_overrides = dict(overrides or {})
        self.current_mol = object()
        self.redraws = 0
        self.styles = []

    def set_3d_style(self, style):
        self.styles.append(style)
        if style != self.current_3d_style:
            self.current_3d_style = style
            self.redraws += 1

    def draw_molecule_3d(self, mol):
        self.redraws += 1


OVERLAY_COORDS = [[0.0, 0.0, 0.0], [1.4, 0.0, 0.0], [-0.5, 0.9, 0.0]]


def overlay_ctx(
    style="ball_and_stick", overrides=None, symbols=("C", "O", "H"), settings=None
):
    ctx = cam_ctx()
    ctx.current_molecule = FakeMol(
        list(symbols), OVERLAY_COORDS, bonds=[(0, 1), (0, 2)]
    )
    v3d = FakeView3D(style, overrides)
    mw = MagicMock()
    mw.view_3d_manager = v3d
    mw.init_manager.settings = (
        settings if settings is not None else {"stick_bond_radius": 0.2}
    )
    ctx.get_main_window.return_value = mw
    rows = [f"{s} {x} {y} {z}" for s, (x, y, z) in zip(symbols, OVERLAY_COORDS)]
    return ctx, v3d, "\n".join(["3", ""] + rows)


@pytest.fixture()
def fake_pv(monkeypatch):
    pv = MagicMock()
    pv.Color.return_value.float_rgb = (0.0, 1.0, 0.0)
    monkeypatch.setitem(sys.modules, "pyvista", pv)
    return pv


def _rgb_arrays(fake_pv):
    """The 'rgb' arrays assigned to point_data, in order (atoms, then bonds)."""
    calls = fake_pv.PolyData.return_value.point_data.__setitem__.call_args_list
    return [c.args[1] for c in calls if c.args[0] == "rgb"]


def test_overlay_switches_to_stick_and_colors_current_carbons(
    bridge_mod, real_numpy, fake_pv
):
    ctx, v3d, text = overlay_ctx()
    result = bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": text, "overlay": True}
    )
    assert result["overlay"] is True
    assert v3d.current_3d_style == "stick"
    assert v3d._plugin_color_overrides == {0: "#3fa7d6"}  # only the carbon
    assert v3d.redraws == 1  # one redraw, not one per atom
    ctx.get_3d_controller.return_value.set_atom_color.assert_not_called()
    names = [kw["name"] for _mesh, kw in ctx.plotter.meshes]
    assert names == ["_mcp_overlay_atoms", "_mcp_overlay_bonds"]
    assert all(
        kw["rgb"] is True and kw["opacity"] < 1 for _mesh, kw in ctx.plotter.meshes
    )


def test_overlay_colors_only_the_other_structures_carbons(
    bridge_mod, real_numpy, fake_pv
):
    ctx, _, text = overlay_ctx()
    bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": text, "overlay": True}
    )
    atom_rgb = _rgb_arrays(fake_pv)[0]
    assert list(atom_rgb[0]) == pytest.approx(
        [1.0, 140 / 255, 0.0]
    )  # C: default dark orange
    assert list(atom_rgb[1]) != list(atom_rgb[0])  # O keeps its own color


def test_overlay_bonds_are_half_colored(bridge_mod, real_numpy, fake_pv):
    ctx, _, text = overlay_ctx()
    bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": text, "overlay": True}
    )
    call = fake_pv.PolyData.call_args_list[1]
    assert len(call.args[0]) == 8  # 2 bonds x 2 halves x 2 points
    assert list(call.kwargs["lines"]) == [2, 0, 1, 2, 2, 3, 2, 4, 5, 2, 6, 7]
    seg_rgb = _rgb_arrays(fake_pv)[1]
    assert list(seg_rgb[0]) == list(seg_rgb[1])  # C half is uniform
    assert list(seg_rgb[2]) != list(seg_rgb[0])  # O half differs


def test_overlay_uses_stick_radius_setting(bridge_mod, real_numpy, fake_pv):
    ctx, _, text = overlay_ctx(settings={"stick_bond_radius": 0.2})
    bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": text, "overlay": True}
    )
    assert fake_pv.Sphere.call_args.kwargs["radius"] == pytest.approx(0.2 * 1.15)


def test_overlay_bad_radius_setting_falls_back(bridge_mod, real_numpy, fake_pv):
    ctx, _, text = overlay_ctx(settings={"stick_bond_radius": "thick"})
    bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": text, "overlay": True}
    )
    assert fake_pv.Sphere.call_args.kwargs["radius"] == pytest.approx(0.15 * 1.15)


def test_overlay_custom_colors(bridge_mod, real_numpy, fake_pv):
    ctx, v3d, text = overlay_ctx()
    bridge_mod.execute_operation(
        ctx,
        "compare_structures",
        {
            "xyz_text": text,
            "overlay": True,
            "overlay_color": "#0000ff",
            "current_color": "#00ff00",
        },
    )
    assert v3d._plugin_color_overrides == {0: "#00ff00"}
    assert list(_rgb_arrays(fake_pv)[0][0]) == pytest.approx([0.0, 0.0, 1.0])


def test_overlay_named_color_goes_through_pyvista(bridge_mod, real_numpy, fake_pv):
    ctx, _, text = overlay_ctx()
    bridge_mod.execute_operation(
        ctx,
        "compare_structures",
        {"xyz_text": text, "overlay": True, "overlay_color": "green"},
    )
    fake_pv.Color.assert_called_with("green")
    assert list(_rgb_arrays(fake_pv)[0][0]) == pytest.approx([0.0, 1.0, 0.0])


def test_clear_overlay_restores_style_and_colors(bridge_mod, real_numpy, fake_pv):
    ctx, v3d, text = overlay_ctx(overrides={0: "#123456", 2: "#abcdef"})
    bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": text, "overlay": True}
    )
    assert v3d._plugin_color_overrides[0] == "#3fa7d6"
    result = bridge_mod.execute_operation(ctx, "clear_overlay", {})
    assert result == {"removed": 2, "restored_style": "ball_and_stick"}
    assert v3d.current_3d_style == "ball_and_stick"
    assert v3d._plugin_color_overrides == {
        0: "#123456",
        2: "#abcdef",
    }  # user's own overrides back


def test_clear_overlay_drops_overrides_it_added(bridge_mod, real_numpy, fake_pv):
    ctx, v3d, text = overlay_ctx()
    bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": text, "overlay": True}
    )
    bridge_mod.execute_operation(ctx, "clear_overlay", {})
    assert v3d._plugin_color_overrides == {}


def test_overlay_twice_then_clear_restores_original(bridge_mod, real_numpy, fake_pv):
    ctx, v3d, text = overlay_ctx(overrides={0: "#123456"})
    bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": text, "overlay": True}
    )
    bridge_mod.execute_operation(
        ctx,
        "compare_structures",
        {"xyz_text": text, "overlay": True, "current_color": "#ffffff"},
    )
    assert v3d._plugin_color_overrides == {0: "#ffffff"}
    bridge_mod.execute_operation(ctx, "clear_overlay", {})
    assert v3d._plugin_color_overrides == {0: "#123456"}
    assert v3d.current_3d_style == "ball_and_stick"


def test_overlay_already_stick_redraws_once_and_restores_stick(
    bridge_mod, real_numpy, fake_pv
):
    ctx, v3d, text = overlay_ctx(style="stick")
    bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": text, "overlay": True}
    )
    assert v3d.redraws == 1
    result = bridge_mod.execute_operation(ctx, "clear_overlay", {})
    assert result["restored_style"] == "stick"
    assert v3d.redraws == 2


def test_overlay_falls_back_to_plugin_context_colors(bridge_mod, real_numpy, fake_pv):
    """Without the manager's override store, colors go through the public API."""
    ctx, _, text = overlay_ctx()
    ctx.get_main_window.return_value.view_3d_manager = None
    bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": text, "overlay": True}
    )
    ctx.get_3d_controller.return_value.set_atom_color.assert_called_once_with(
        0, "#3fa7d6"
    )
    bridge_mod.execute_operation(ctx, "clear_overlay", {})
    ctx.get_3d_controller.return_value.set_atom_color.assert_called_with(0, None)


def test_overlay_without_carbons_changes_no_colors(bridge_mod, real_numpy, fake_pv):
    ctx, v3d, text = overlay_ctx(symbols=("N", "O", "H"))
    bridge_mod.execute_operation(
        ctx, "compare_structures", {"xyz_text": text, "overlay": True}
    )
    assert v3d._plugin_color_overrides == {}
    assert v3d.current_3d_style == "stick"


def test_clear_overlay_without_overlay_is_harmless(bridge_mod):
    ctx, v3d, _ = overlay_ctx(overrides={1: "#111111"})
    result = bridge_mod.execute_operation(ctx, "clear_overlay", {})
    assert "restored_style" not in result
    assert v3d._plugin_color_overrides == {1: "#111111"}
    assert v3d.styles == []


def test_element_rgb_fallback_table(bridge_mod):
    assert bridge_mod._element_rgb("O") != bridge_mod._element_rgb("N")
    assert bridge_mod._element_rgb("Xx") == (0.75, 0.75, 0.75)


def test_hex_rgb(bridge_mod):
    assert bridge_mod._hex_rgb("#ff0000") == (1.0, 0.0, 0.0)


def test_clear_overlay_without_viewer(bridge_mod):
    ctx = make_context()
    ctx.plotter = None
    assert bridge_mod.execute_operation(ctx, "clear_overlay", {}) == {"removed": 0}


@pytest.mark.parametrize(
    "text,msg",
    [
        ("", "no atoms"),
        ("2\n\nO 0 0 0\nH 0 0 1", "Atom count"),
        ("3\n\nH 0 0 0\nO 0 0 1\nH 0 1 0", "Atom order"),
    ],
)
def test_compare_rejects_mismatch(bridge_mod, real_numpy, text, msg):
    with pytest.raises(ValueError, match=msg):
        bridge_mod.execute_operation(
            cmp_ctx(), "compare_structures", {"xyz_text": text}
        )


def test_compare_frame_out_of_range(bridge_mod, real_numpy):
    with pytest.raises(ValueError, match="out of range"):
        bridge_mod.execute_operation(
            cmp_ctx(), "compare_structures", {"xyz_text": water_xyz(), "frame": 3}
        )


def test_compare_no_current_3d(bridge_mod, real_numpy):
    ctx = make_context()
    ctx.current_molecule = None
    with pytest.raises(ValueError, match="No 3D"):
        bridge_mod.execute_operation(
            ctx, "compare_structures", {"xyz_text": water_xyz()}
        )

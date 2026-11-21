"""Loading a molecule replaces the 2D canvas unless ``clear=false``.

MoleditPy's SMILES import adds to the 2D scene, so without clearing, a
second load (or a load after another plugin opened a file and left the old
structure on the canvas) merged both molecules into one drawing. The fake
context below models that additive canvas.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from conftest import load_module, mock_optional_imports


@pytest.fixture()
def bridge_mod():
    with mock_optional_imports():
        yield load_module("bridge.py")


@pytest.fixture()
def srv():
    with mock_optional_imports():
        yield load_module("server.py")


class _AdditiveCanvasContext:
    """PluginContext stand-in whose SMILES load adds to the canvas, like the app."""

    def __init__(self) -> None:
        self.canvas: list[str] = []
        self.current_molecule: Any = None
        self.undo: list[tuple[str, ...]] = []

    def get_main_window(self) -> None:
        return None

    def load_from_smiles(self, smiles: str) -> None:
        self.canvas.append(smiles)
        self.undo.append(tuple(self.canvas))

    def clear_canvas(self, push_to_undo: bool = True) -> None:
        self.canvas.clear()
        self.current_molecule = None
        if push_to_undo:
            self.undo.append(())

    def push_undo_checkpoint(self) -> None:
        self.undo.append(tuple(self.canvas))

    def refresh_ui(self) -> None:
        pass


def _bridge_for(bridge_mod: Any, ctx: Any) -> MagicMock:
    """A bridge whose call() runs the real operation on *ctx*."""
    bridge = MagicMock()
    bridge.call.side_effect = lambda op, args=None, timeout=10.0: (
        bridge_mod.execute_operation(ctx, op, args or {})
    )
    return bridge


# ---------------------------------------------------------------------------
# Bridge operations
# ---------------------------------------------------------------------------


def test_loading_twice_keeps_only_the_second_molecule(bridge_mod):
    ctx = _AdditiveCanvasContext()
    bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "ibuprofen"})
    result = bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "caffeine"})
    assert ctx.canvas == ["caffeine"]
    assert result == {"success": True, "cleared": True}


def test_clear_pushes_an_undo_checkpoint_so_the_old_molecule_is_recoverable(
    bridge_mod,
):
    ctx = _AdditiveCanvasContext()
    bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "ibuprofen"})
    bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "caffeine"})
    # cleared, ibuprofen, cleared, caffeine: undo twice returns to ibuprofen.
    assert ctx.undo == [(), ("ibuprofen",), (), ("caffeine",)]


def test_clear_false_keeps_the_old_molecule(bridge_mod):
    ctx = _AdditiveCanvasContext()
    bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "ibuprofen"})
    result = bridge_mod.execute_operation(
        ctx, "load_smiles", {"smiles": "caffeine", "clear": False}
    )
    assert ctx.canvas == ["ibuprofen", "caffeine"]
    assert result["cleared"] is False


def test_clear_null_means_the_default(bridge_mod):
    ctx = _AdditiveCanvasContext()
    bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "ibuprofen"})
    bridge_mod.execute_operation(
        ctx, "load_smiles", {"smiles": "caffeine", "clear": None}
    )
    assert ctx.canvas == ["caffeine"]


@pytest.mark.parametrize("bad", ["false", 0, 1, "yes"])
def test_clear_must_be_a_boolean(bridge_mod, bad):
    ctx = _AdditiveCanvasContext()
    bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "ibuprofen"})
    with pytest.raises(ValueError, match="'clear' must be a boolean"):
        bridge_mod.execute_operation(
            ctx, "load_smiles", {"smiles": "caffeine", "clear": bad}
        )
    # Rejected before touching the canvas.
    assert ctx.canvas == ["ibuprofen"]


def _with_mol_block_parser(parsed: Any):
    chem_mock = MagicMock(name="Chem")
    chem_mock.MolFromMolBlock.return_value = parsed
    rdkit_mock = MagicMock(name="rdkit")
    rdkit_mock.Chem = chem_mock
    return patch.dict(sys.modules, {"rdkit": rdkit_mock, "rdkit.Chem": chem_mock})


def test_mol_block_load_clears_the_stale_2d_drawing(bridge_mod):
    ctx = _AdditiveCanvasContext()
    bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "ibuprofen"})
    new_mol = object()
    with _with_mol_block_parser(new_mol):
        result = bridge_mod.execute_operation(
            ctx, "load_mol_block", {"mol_block": "caffeine\n...\nM  END"}
        )
    assert result == {"success": True, "cleared": True}
    assert ctx.canvas == []
    assert ctx.current_molecule is new_mol


def test_mol_block_load_with_clear_false_keeps_the_drawing(bridge_mod):
    ctx = _AdditiveCanvasContext()
    bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "ibuprofen"})
    new_mol = object()
    with _with_mol_block_parser(new_mol):
        result = bridge_mod.execute_operation(
            ctx, "load_mol_block", {"mol_block": "x\nM  END", "clear": False}
        )
    assert result["cleared"] is False
    assert ctx.canvas == ["ibuprofen"]
    assert ctx.current_molecule is new_mol


def test_unparsable_mol_block_leaves_the_canvas_alone(bridge_mod):
    ctx = _AdditiveCanvasContext()
    bridge_mod.execute_operation(ctx, "load_smiles", {"smiles": "ibuprofen"})
    with _with_mol_block_parser(None):
        result = bridge_mod.execute_operation(
            ctx, "load_mol_block", {"mol_block": "garbage"}
        )
    assert result == {"success": False}
    assert ctx.canvas == ["ibuprofen"]


# ---------------------------------------------------------------------------
# Tools, end to end through dispatch_tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    ["load_molecule_from_smiles", "load_molecule_by_name", "load_from_mol_block"],
)
def test_load_tools_advertise_clear(srv, tool):
    schema = next(t for t in srv._TOOLS if t["name"] == tool)["inputSchema"]
    assert schema["properties"]["clear"]["type"] == "boolean"
    assert "clear" not in schema.get("required", [])
    assert (
        "clear=false" in next(t for t in srv._TOOLS if t["name"] == tool)["description"]
    )


def test_server_instructions_mention_clear(srv):
    assert "clear=false" in srv._SERVER_INSTRUCTIONS


def test_smiles_tool_twice_gives_only_the_second_molecule(srv, bridge_mod):
    ctx = _AdditiveCanvasContext()
    bridge = _bridge_for(bridge_mod, ctx)
    srv.dispatch_tool(bridge, "load_molecule_from_smiles", {"smiles": "CCO"})
    srv.dispatch_tool(bridge, "load_molecule_from_smiles", {"smiles": "c1ccccc1"})
    assert ctx.canvas == ["c1ccccc1"]


def test_name_tool_twice_gives_only_the_second_molecule(srv, bridge_mod):
    ctx = _AdditiveCanvasContext()
    bridge = _bridge_for(bridge_mod, ctx)
    lookup = {
        "ibuprofen": "CC(C)Cc1ccc(C(C)C(=O)O)cc1",
        "caffeine": "Cn1cnc2c1c(=O)n(C)c(=O)n2C",
    }
    with patch.object(srv, "_fetch_smiles_by_name", side_effect=lookup.__getitem__):
        srv.dispatch_tool(bridge, "load_molecule_by_name", {"name": "ibuprofen"})
        result = srv.dispatch_tool(
            bridge, "load_molecule_by_name", {"name": "caffeine"}
        )
    assert result.get("isError") is not True
    assert ctx.canvas == [lookup["caffeine"]]


def test_name_tool_clear_false_keeps_the_old_molecule(srv, bridge_mod):
    ctx = _AdditiveCanvasContext()
    bridge = _bridge_for(bridge_mod, ctx)
    with patch.object(srv, "_fetch_smiles_by_name", side_effect=["A", "B"]):
        srv.dispatch_tool(bridge, "load_molecule_by_name", {"name": "a"})
        srv.dispatch_tool(
            bridge, "load_molecule_by_name", {"name": "b", "clear": False}
        )
    assert ctx.canvas == ["A", "B"]


def test_failed_name_lookup_does_not_clear_the_canvas(srv, bridge_mod):
    ctx = _AdditiveCanvasContext()
    bridge = _bridge_for(bridge_mod, ctx)
    srv.dispatch_tool(bridge, "load_molecule_from_smiles", {"smiles": "CCO"})
    with patch.object(
        srv, "_fetch_smiles_by_name", side_effect=ValueError("not found")
    ):
        result = srv.dispatch_tool(bridge, "load_molecule_by_name", {"name": "zzz"})
    assert result.get("isError") is True
    assert ctx.canvas == ["CCO"]

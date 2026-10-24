import os
import base64
from unittest.mock import MagicMock

import pytest
from conftest import load_module, mock_optional_imports


@pytest.mark.parametrize("tool", ["write_text_file", "write_file_with_xyz_block"])
def test_overwrite_does_not_modify_outside_hardlink(tmp_path, tool):
    with mock_optional_imports():
        srv = load_module("server.py")
    root = tmp_path / "sandbox"
    root.mkdir()
    victim = tmp_path / "outside.txt"
    victim.write_text("keep me")
    target = root / "output.txt"
    os.link(victim, target)
    bridge = MagicMock()
    bridge.call.side_effect = lambda operation, *a, **k: {
        "get_file_io_config": {"base_dir": str(root), "allowed_extensions": [".txt"]},
        "get_xyz_atoms": {
            "has_data": True,
            "atoms": [
                {"index": 0, "symbol": "H", "atomic_num": 1, "x": 0, "y": 0, "z": 0}
            ],
        },
    }[operation]
    result = srv.dispatch_tool(
        bridge, tool, {"path": "output.txt", "content": "new", "overwrite": True}
    )
    assert not result.get("isError")
    assert victim.read_text() == "keep me"
    assert target.read_text() != "keep me"


def test_atomic_publish_refuses_concurrent_existing_file(tmp_path):
    with mock_optional_imports():
        srv = load_module("server.py")
    target = tmp_path / "output.txt"
    target.write_text("created by another writer")
    with pytest.raises(FileExistsError):
        srv._write_sandbox_file(target, "new", overwrite=False)
    assert target.read_text() == "created by another writer"


def test_image_overwrite_does_not_modify_outside_hardlink(tmp_path):
    with mock_optional_imports():
        srv = load_module("server.py")
    root = tmp_path / "sandbox"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"original outside sandbox")
    target = root / "image.png"
    os.link(outside, target)
    bridge = MagicMock()
    bridge.call.side_effect = lambda operation, *a, **k: {
        "get_file_io_config": {"base_dir": str(root)},
        "get_molecule_image": {
            "image_base64": base64.b64encode(b"new image").decode(),
            "view": "3d",
            "width": 10,
            "height": 10,
        },
    }[operation]
    result = srv.dispatch_tool(
        bridge, "save_molecule_image", {"path": "image.png", "overwrite": True}
    )
    assert not result.get("isError")
    assert outside.read_bytes() == b"original outside sandbox"
    assert target.read_bytes() == b"new image"


def test_image_save_does_not_overwrite_file_created_during_render(tmp_path):
    with mock_optional_imports():
        srv = load_module("server.py")
    target = tmp_path / "image.png"
    bridge = MagicMock()

    def call(operation, *args, **kwargs):
        if operation == "get_file_io_config":
            return {"base_dir": str(tmp_path)}
        assert operation == "get_molecule_image"
        target.write_bytes(b"concurrent image")
        return {
            "image_base64": base64.b64encode(b"new image").decode(),
            "view": "3d",
            "width": 10,
            "height": 10,
        }

    bridge.call.side_effect = call
    result = srv.dispatch_tool(bridge, "save_molecule_image", {"path": "image.png"})
    assert result["isError"]
    assert target.read_bytes() == b"concurrent image"

import os
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

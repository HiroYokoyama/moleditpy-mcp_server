"""Static check: every mw.* / manager.* / ctx.* access in mcp_server/ exists
in the main app (tests/plugin_api_checker.py, master copy in
moleditpy-plugins/api-checker/).

The app source is taken from a sibling python_molecular_editor checkout, or
else from an installed moleditpy package (the CI integration job installs
it with pip). Skipped when neither is available.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _TESTS_DIR.parent
_WORKSPACE_ROOT = _PLUGIN_ROOT.parent
_SCAN_DIR = _PLUGIN_ROOT / "mcp_server"


def _installed_app_root():
    """Directory of an installed moleditpy package (its source is plain .py)."""
    try:
        spec = importlib.util.find_spec("moleditpy")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    root = Path(spec.origin).resolve().parent
    return root if (root / "plugins" / "plugin_interface.py").exists() else None


def _app_root():
    for candidate in (_WORKSPACE_ROOT / "python_molecular_editor", _PLUGIN_ROOT / "python_molecular_editor"):
        if (candidate / "moleditpy").exists():
            return candidate / "moleditpy" / "src" / "moleditpy"
    return _installed_app_root()


_APP_PATH = _app_root()


def _load_checker():
    checker_path = _TESTS_DIR / "plugin_api_checker.py"
    spec = importlib.util.spec_from_file_location("plugin_api_checker", checker_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(checker_path)
    spec.loader.exec_module(mod)
    return mod


class TestAPIChecker(unittest.TestCase):
    @unittest.skipUnless(_APP_PATH is not None, "MoleditPy source not found (sibling checkout or installed package)")
    def test_no_unknown_api_accesses(self):
        checker_mod = _load_checker()
        api = checker_mod.AppAPIExtractor(_APP_PATH, verbose=False).extract()
        allowlist = checker_mod._merge_allowlists(
            checker_mod._MANAGER_ALLOWLIST,
            checker_mod._MW_ALLOWLIST,
            checker_mod._load_site_allowlist(_PLUGIN_ROOT),
        )
        issues = []
        for path in sorted(_SCAN_DIR.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            issues += checker_mod.PluginFileChecker(
                path, api, check_context=True, allowlist=allowlist
            ).check()
        if issues:
            lines = [
                f"  [{i.code}] {Path(i.file).relative_to(_PLUGIN_ROOT)} line {i.line}: {i.message}"
                for i in issues
            ]
            self.fail(f"{len(issues)} unknown API access(es):\n" + "\n".join(lines))

    def test_scan_dir_exists(self):
        self.assertTrue((_SCAN_DIR / "bridge.py").exists())


if __name__ == "__main__":
    unittest.main()

"""Plugin-local settings with a one-time import of legacy host settings."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_LEGACY_TYPES = {
    "auth_token": (str,),
    "auto_start": (bool,),
    "port": (int,),
    "protocol_mode": (str,),
    "file_io_base_dir": (str, type(None)),
    "file_io_allowed_extensions": (list,),
    "file_io_read_roots": (list,),
}


class SettingsContext:
    """Delegate host operations while keeping settings in the plugin folder."""

    def __init__(self, context: Any, path: Path) -> None:
        self._context = context
        self.path = Path(path)
        with _LOCK:
            if self.path.exists():
                self._read()
            else:
                missing = object()
                data = {}
                for key, kinds in _LEGACY_TYPES.items():
                    value = context.get_setting(key, missing)
                    if value is not missing and type(value) in kinds:
                        data[key] = value
                self._write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    def _read(self) -> dict:
        # A damaged file must not reset the token or file-access restrictions.
        with self.path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise TypeError(f"MCP settings must be a JSON object: {self.path}")
        return data

    def _write(self, data: dict) -> None:
        content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        handle, temporary = tempfile.mkstemp(
            prefix=".settings-", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get_setting(self, key: str, default: Any = None) -> Any:
        with _LOCK:
            return self._read().get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        with _LOCK:
            data = self._read()
            data[key] = value
            self._write(data)

"""Tiny JSON-on-disk cache used for HTTP responses and LLM results."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


class JsonDiskCache:
    def __init__(self, directory: str | Path, ttl_hours: float | None = None) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = None if ttl_hours is None else ttl_hours * 3600

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.directory / digest[:2] / f"{digest}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None  # a corrupt entry is treated as a miss and overwritten later
        if (
            self.ttl_seconds is not None
            and time.time() - entry.get("_stored_at", 0) > self.ttl_seconds
        ):
            return None
        return entry.get("value")

    def set(self, key: str, value: dict[str, Any]) -> None:
        path = self._path(key)
        # Owner-only: cached pages are third-party content and may include names.
        for directory in (self.directory, path.parent):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        tmp = path.with_suffix(".tmp")
        tmp.touch(mode=0o600)
        os.chmod(tmp, 0o600)
        tmp.write_text(
            json.dumps(
                {"_key": key, "_stored_at": time.time(), "value": value}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)  # atomic, so an interrupted run never leaves half a file

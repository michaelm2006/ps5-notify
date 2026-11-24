from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from ps5_notify.models import RetailerState

logger = logging.getLogger(__name__)


class StateStore:
    """Atomic JSON persistence for per-retailer stock & backoff state."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._states: dict[str, RetailerState] = {}
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        self._states = {}
        if not self._path.exists():
            return

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load state file %s (%s); starting with fresh state.", self._path, exc)
            self._backup_corrupt_file()
            return

        if not isinstance(raw, dict):
            logger.warning("Unexpected JSON root in %s; starting with fresh state.", self._path)
            self._backup_corrupt_file()
            return

        retailers_blob = raw.get("retailers", raw)
        if not isinstance(retailers_blob, dict):
            return

        for key, value in retailers_blob.items():
            if isinstance(key, str) and isinstance(value, dict):
                self._states[key] = RetailerState.from_dict(value)

    def _backup_corrupt_file(self) -> None:
        backup_path = self._path.with_suffix(self._path.suffix + ".corrupt")
        try:
            os.replace(self._path, backup_path)
            logger.warning("Backed up corrupt state file to %s", backup_path)
        except OSError:
            pass

    def get(self, retailer_key: str) -> RetailerState:
        if retailer_key not in self._states:
            self._states[retailer_key] = RetailerState()
        return self._states[retailer_key]

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": 1,
            "retailers": {key: state.to_dict() for key, state in sorted(self._states.items())},
        }
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

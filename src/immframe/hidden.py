"""Local "never show again" list.

Asset IDs the user has hidden from the frame. Kept locally so the action
is instant and works even when the Immich key can't write (the controller
*also* archives the asset in Immich when it can, which is the durable,
visible-in-Immich version of the same intent).

Persisted as JSON at `$XDG_STATE_HOME/immframe/hidden.json` (default
`~/.local/state/immframe/hidden.json`). Writes are atomic (tmp + rename)
and the file is only rewritten on change, so a frame that never hides
anything never touches it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger(__name__)


def default_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "immframe" / "hidden.json"


class HiddenList:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_path()
        self._lock = threading.Lock()
        self._ids: set[str] = set()
        self._load()

    # ── Query ───────────────────────────────────────────────────────────
    def __contains__(self, asset_id: str) -> bool:
        with self._lock:
            return asset_id in self._ids

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)

    @property
    def path(self) -> Path:
        return self._path

    # ── Mutate ──────────────────────────────────────────────────────────
    def add(self, *asset_ids: str) -> bool:
        """Hide the given IDs. Returns True if anything changed."""
        new = {a for a in asset_ids if isinstance(a, str) and a}
        with self._lock:
            if new <= self._ids:
                return False
            self._ids |= new
            self._save()
        return True

    # ── Persistence ─────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("hidden list %s unreadable (%s) — starting empty", self._path, e)
            return
        ids = data.get("hidden") if isinstance(data, dict) else data
        if isinstance(ids, list):
            self._ids = {a for a in ids if isinstance(a, str)}
        log.info("hidden list: %d asset(s) from %s", len(self._ids), self._path)

    def _save(self) -> None:
        # Called with the lock held.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".part")
            tmp.write_text(json.dumps({"hidden": sorted(self._ids)}, indent=1))
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("could not save hidden list to %s: %s", self._path, e)

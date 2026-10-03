"""Local "never show again" (block) list.

Asset IDs the user has blocked from the frame. Kept locally so the action
is instant and works even when the Immich key can't write (the controller
*also* archives the asset in Immich when it can, which is the durable,
visible-in-Immich version of the same intent — and un-archives it again
on unblock).

Each block has a *primary* asset id plus optional companions (a live
photo's motion clip), and details for the dashboard's Blocked page:
original file name, kind, when it was blocked, whether Immich archived it.

Persisted as JSON at `$XDG_STATE_HOME/immframe/hidden.json` (default
`~/.local/state/immframe/hidden.json`):

    {"hidden": ["id", ...],                     # every blocked id (primaries + companions)
     "details": {"<primary id>": {"file": ..., "kind": ..., "at": epoch,
                                  "archived": bool, "companions": [...]}}}

The older plain format (`{"hidden": [...]}` or a bare list) still loads;
entries without details are listed by id. Writes are atomic (tmp + rename)
and only happen on change.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(base).expanduser() / "immframe"


def default_path() -> Path:
    return state_dir() / "hidden.json"


class HiddenList:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_path()
        self._lock = threading.Lock()
        self._ids: set[str] = set()
        self._details: dict[str, dict[str, Any]] = {}
        self._load()

    # ── Query ───────────────────────────────────────────────────────────
    def __contains__(self, asset_id: str) -> bool:
        with self._lock:
            return asset_id in self._ids

    def __len__(self) -> int:
        """Number of blocks (a live photo and its clip count once)."""
        with self._lock:
            return len(self._primaries())

    @property
    def path(self) -> Path:
        return self._path

    def entries(self) -> list[dict[str, Any]]:
        """Blocks for display, newest first (legacy id-only entries last)."""
        with self._lock:
            out = []
            for aid in self._primaries():
                d = self._details.get(aid, {})
                out.append({
                    "id": aid,
                    "file": d.get("file"),
                    "kind": d.get("kind"),
                    "at": d.get("at"),
                    "archived": bool(d.get("archived", False)),
                    "companions": list(d.get("companions", [])),
                })
        out.sort(key=lambda e: e["at"] or 0, reverse=True)
        return out

    def details(self, asset_id: str) -> dict[str, Any] | None:
        with self._lock:
            d = self._details.get(asset_id)
            return dict(d) if d is not None else None

    def _primaries(self) -> list[str]:
        companions = {c for d in self._details.values() for c in d.get("companions", [])}
        return [a for a in self._ids if a not in companions]

    # ── Mutate ──────────────────────────────────────────────────────────
    def add(self, *asset_ids: str, details: dict[str, Any] | None = None) -> bool:
        """Block the given IDs. The first id is the primary; `details`
        (file / kind / companions …) are attached to it. Returns True if
        anything changed."""
        new = [a for a in asset_ids if isinstance(a, str) and a]
        if not new:
            return False
        with self._lock:
            changed = not set(new) <= self._ids
            self._ids.update(new)
            if details is not None or new[0] not in self._details:
                d = dict(self._details.get(new[0], {}))
                d.update(details or {})
                d.setdefault("at", time.time())
                comps = set(d.get("companions", [])) | set(new[1:])
                if comps:
                    d["companions"] = sorted(comps)
                if d != self._details.get(new[0]):
                    self._details[new[0]] = d
                    changed = True
            if changed:
                self._save()
        return changed

    def annotate(self, asset_id: str, **fields: Any) -> None:
        """Merge fields into an existing block's details (e.g. archived=True
        once Immich accepted the archive)."""
        with self._lock:
            if asset_id not in self._ids:
                return
            d = self._details.setdefault(asset_id, {})
            if all(d.get(k) == v for k, v in fields.items()):
                return
            d.update(fields)
            self._save()

    def remove(self, asset_id: str) -> dict[str, Any] | None:
        """Unblock an id (and its companions). Returns the removed block's
        details ({} for a legacy entry), or None if it wasn't blocked."""
        with self._lock:
            if asset_id not in self._ids:
                return None
            d = self._details.pop(asset_id, {})
            self._ids.discard(asset_id)
            for c in d.get("companions", []):
                self._ids.discard(c)
            self._save()
            return d

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
        details = data.get("details") if isinstance(data, dict) else None
        if isinstance(details, dict):
            self._details = {
                k: v for k, v in details.items()
                if isinstance(k, str) and isinstance(v, dict) and k in self._ids
            }
        log.info("hidden list: %d asset(s) from %s", len(self._ids), self._path)

    def _save(self) -> None:
        # Called with the lock held.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".part")
            payload: dict[str, Any] = {"hidden": sorted(self._ids)}
            if self._details:
                payload["details"] = self._details
            tmp.write_text(json.dumps(payload, indent=1))
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("could not save hidden list to %s: %s", self._path, e)

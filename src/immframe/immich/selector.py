"""Asset-selection strategies.

Three concrete strategies share a single `AssetSelector` Protocol. The
controller swaps the active selector at runtime by replacing the reference
inside the prefetch worker.

Contract:
- `next_batch(n)` returns up to `n` assets. Returning fewer is allowed.
  Returning `[]` signals "nothing matched right now" — the prefetch worker
  will back off and retry.
- Selectors don't de-duplicate across calls.
- Selectors may make blocking network calls; the prefetch worker runs them
  off the render thread.

Caching: selectors that need pagination/shuffling (album) handle that
internally — no knobs exposed. Refetch when the local pool is exhausted.
"""
from __future__ import annotations

import logging
import random
import threading
from typing import Literal, Protocol, runtime_checkable

from datetime import datetime, timedelta, timezone

from .client import ImmichClient, ImmichError, _to_asset
from .models import Asset

log = logging.getLogger(__name__)


@runtime_checkable
class AssetSelector(Protocol):
    def next_batch(self, n: int) -> list[Asset]: ...


class RandomSelector:
    """Whole-library random via Immich `/search/random`.

    Immich already returns a random sample, so we just pass `n` through.
    """

    def __init__(self, client: ImmichClient, *, include_videos: bool = True) -> None:
        self._client = client
        self._include_videos = include_videos

    def next_batch(self, n: int) -> list[Asset]:
        try:
            return self._client.random_assets(n, with_video=self._include_videos)
        except ImmichError as e:
            log.warning("random_assets failed: %s", e)
            return []


class AlbumSelector:
    """Random shuffle within one or more albums.

    Internally: fetches each album's asset list lazily, shuffles them
    together into a single pool, iterates, refetches when exhausted.
    `set_album_ids()` invalidates the local pool.
    """

    def __init__(self, client: ImmichClient, album_ids: list[str]) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._album_ids: list[str] = list(album_ids)
        self._pool: list[Asset] = []

    def set_album_ids(self, album_ids: list[str]) -> None:
        with self._lock:
            self._album_ids = list(album_ids)
            self._pool = []

    def next_batch(self, n: int) -> list[Asset]:
        with self._lock:
            if not self._album_ids:
                return []
            if not self._pool:
                self._pool = self._refill()
            if not self._pool:
                return []
            take = min(n, len(self._pool))
            out = self._pool[:take]
            self._pool = self._pool[take:]
            return out

    def _refill(self) -> list[Asset]:
        assets: list[Asset] = []
        for aid in self._album_ids:
            try:
                assets.extend(self._client.album_assets(aid))
            except ImmichError as e:
                log.warning("album_assets(%s) failed: %s", aid, e)
        random.shuffle(assets)
        return assets


class SmartSelector:
    """CLIP smart-search-driven selection.

    Calls Immich's smart search per batch. `set_query()` takes effect on the
    next call.
    """

    def __init__(self, client: ImmichClient, query: str) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._query = query

    def set_query(self, query: str) -> None:
        with self._lock:
            self._query = query

    def next_batch(self, n: int) -> list[Asset]:
        with self._lock:
            q = self._query
        if not q:
            return []
        try:
            return self._client.search_smart(q, count=n)
        except ImmichError as e:
            log.warning("search_smart failed: %s", e)
            return []


class PeopleSelector:
    """Slideshow of photos featuring specific people.

    Three modes via the `person_ids` argument:

    - **specific list**: only photos with at least one of these named people
    - **empty list**: rotate through ALL named people in the library, one
      person at a time (each rotation = one person's photo pool)

    When a single person is selected, the slideshow draws ~`pool_size`
    photos of them, then rotates to the next. With an empty list this gives
    a long, varied tour of every family member; with a curated list it
    becomes a focused "just my kids" or "Alice + Bob" frame.

    Uses `/search/metadata` with `personIds` filter — server-side. We never
    download the full library and filter client-side.

    `current_scene` exposes the currently-rotating person's NAME (not ID)
    so the controller surfaces it the same way as scene mode.
    """

    def __init__(
        self,
        client: ImmichClient,
        person_ids: list[str] | None = None,
        *,
        pool_size: int = 25,
    ) -> None:
        self._client = client
        self._explicit_ids = list(person_ids or [])
        self._pool_size = pool_size
        self._lock = threading.Lock()
        # Lazily populated:
        self._person_index: dict[str, str] = {}      # id → name (for label exposure)
        self._rotation_ids: list[str] = []           # ids we cycle through
        self._current_name: str | None = None
        self._pool: list[Asset] = []

    @property
    def current_scene(self) -> str | None:
        """Returns the currently-rotating person's name (or None)."""
        with self._lock:
            return self._current_name

    def set_person_ids(self, person_ids: list[str]) -> None:
        with self._lock:
            self._explicit_ids = list(person_ids)
            self._person_index = {}
            self._rotation_ids = []
            self._pool = []

    def next_batch(self, n: int) -> list[Asset]:
        with self._lock:
            if not self._pool:
                self._rotate()
            if not self._pool:
                return []
            take = min(n, len(self._pool))
            out = self._pool[:take]
            self._pool = self._pool[take:]
            return out

    def _rotate(self) -> None:
        if not self._rotation_ids:
            self._rotation_ids = self._resolve_rotation_ids()
            if not self._rotation_ids:
                log.warning(
                    "people mode: no person IDs to rotate through. "
                    "Set selection.people_ids to specific UUIDs, or tag "
                    "people in Immich so the auto-rotation has someone to pick."
                )
                return

        person_id = random.choice(self._rotation_ids)
        self._current_name = self._person_index.get(person_id) or person_id
        log.info("people rotation -> %r (%s)", self._current_name, person_id)
        try:
            pool = self._client.search_metadata(
                person_ids=[person_id], count=self._pool_size,
            )
        except ImmichError as e:
            log.warning("people search_metadata for %s failed: %s", person_id, e)
            pool = []
        random.shuffle(pool)
        self._pool = pool

    def _resolve_rotation_ids(self) -> list[str]:
        if self._explicit_ids:
            # Resolve display names for log/UI; if /people fails we still
            # rotate, just without nice names.
            try:
                people = self._client.list_people()
                index = {p["id"]: p["name"] for p in people if p.get("id") and p.get("name")}
                self._person_index = {pid: index.get(pid, pid) for pid in self._explicit_ids}
            except ImmichError as e:
                log.debug("list_people for label lookup failed: %s", e)
                self._person_index = {pid: pid for pid in self._explicit_ids}
            return list(self._explicit_ids)

        # No explicit list: rotate every named, non-hidden person
        try:
            people = self._client.list_people()
        except ImmichError as e:
            log.warning("list_people failed: %s", e)
            return []
        named = [p for p in people if p.get("name") and not p.get("isHidden") and p.get("id")]
        if not named:
            return []
        self._person_index = {p["id"]: p["name"] for p in named}
        return [p["id"] for p in named]


class MemorySelector:
    """On-this-day slideshow driven by Immich's /memories endpoint.

    Each rotation picks a random memory (typically "on this day N years
    ago"), shuffles its assets, and shows them in order. The memory list
    itself refreshes when exhausted (each memory has a handful of assets,
    so we cycle through many memories quickly).

    `current_scene` exposes a friendly label like "On this day — 5 years
    ago" so the controller can publish it.

    Note: assets returned by /memories don't carry exifInfo by default —
    overlay city/country/camera will be blank for memory-mode slides
    unless we re-fetch each asset. Kept simple for v1.
    """

    def __init__(self, client: ImmichClient, *, pool_size: int = 25) -> None:
        self._client = client
        self._pool_size = pool_size
        self._lock = threading.Lock()
        self._memories: list[dict] = []
        self._pool: list[Asset] = []
        self._current_label: str | None = None

    @property
    def current_scene(self) -> str | None:
        with self._lock:
            return self._current_label

    def next_batch(self, n: int) -> list[Asset]:
        with self._lock:
            if not self._pool:
                self._rotate()
            if not self._pool:
                return []
            take = min(n, len(self._pool))
            out = self._pool[:take]
            self._pool = self._pool[take:]
            return out

    def _rotate(self) -> None:
        if not self._memories:
            try:
                self._memories = self._client.list_memories()
            except ImmichError as e:
                log.warning("list_memories failed: %s", e)
                return
        if not self._memories:
            log.info("no memories returned — Immich's memory generation may not have run yet")
            return

        mem = random.choice(self._memories)
        asset_dicts = mem.get("assets") or []
        pool = [_to_asset(a) for a in asset_dicts if isinstance(a, dict)]
        random.shuffle(pool)
        self._pool = pool

        # Friendly label
        data = mem.get("data") or {}
        year = data.get("year") if isinstance(data, dict) else None
        if isinstance(year, int):
            age = datetime.now().year - year
            self._current_label = f"On this day — {age} year{'s' if age != 1 else ''} ago"
        else:
            mem_at = (mem.get("memoryAt") or "")[:10]
            self._current_label = f"Memory: {mem_at}" if mem_at else "Memory"
        log.info("memory rotation -> %r (%d assets)", self._current_label, len(pool))


class RecentSelector:
    """Random within recently-uploaded photos.

    Looks at assets uploaded (`createdAfter`) to Immich within the last
    `days` window. Re-queries on every rotation so newly-uploaded photos
    surface quickly.

    For "taken in the last N days" instead of "uploaded in the last N
    days", set `field="taken"`.
    """

    def __init__(
        self,
        client: ImmichClient,
        *,
        days: int = 30,
        field: str = "created",     # 'created' (uploaded) or 'taken'
        pool_size: int = 25,
    ) -> None:
        if field not in ("created", "taken"):
            raise ValueError(f"RecentSelector.field must be 'created' or 'taken'; got {field!r}")
        self._client = client
        self._days = max(1, int(days))
        self._field = field
        self._pool_size = pool_size

    @property
    def current_scene(self) -> str | None:
        return f"Last {self._days} days"

    def next_batch(self, n: int) -> list[Asset]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._days)
        try:
            if self._field == "taken":
                assets = self._client.search_metadata(taken_after=cutoff, count=n)
            else:
                assets = self._client.search_metadata(created_after=cutoff, count=n)
        except ImmichError as e:
            log.warning("recent search failed: %s", e)
            return []
        random.shuffle(assets)
        return assets


class PlaylistSelector:
    """Round-robins through a sequence of (selector, count) entries.

    On each `next_batch(n)` call, draws up to `n` assets from the current
    entry's selector, counting toward its `count` quota. When the quota
    fills or the sub-selector returns nothing, advances to the next entry.
    Cycles indefinitely.

    Useful for "show 25 random, then 25 on-this-day, then 25 of Alice,
    repeat" without picking just one mode.
    """

    def __init__(self, entries: list[tuple[AssetSelector, int]]) -> None:
        if not entries:
            raise ValueError("PlaylistSelector requires at least one entry")
        self._entries = list(entries)
        self._lock = threading.Lock()
        self._idx = 0
        self._consumed_this_round = 0

    @property
    def current_scene(self) -> str | None:
        with self._lock:
            sel, _ = self._entries[self._idx]
        # Expose the inner selector's label if it has one
        return getattr(sel, "current_scene", None)

    def next_batch(self, n: int) -> list[Asset]:
        with self._lock:
            # Try every entry once before giving up — covers the case where
            # the first few are empty (recent with no new uploads, etc.)
            for _ in range(len(self._entries)):
                sel, cnt = self._entries[self._idx]
                remaining = max(0, cnt - self._consumed_this_round)
                take = min(n, remaining)
                if take == 0:
                    self._advance()
                    continue
                batch = sel.next_batch(take)
                if not batch:
                    self._advance()
                    continue
                self._consumed_this_round += len(batch)
                if self._consumed_this_round >= cnt:
                    self._advance()
                return batch
        return []

    def _advance(self) -> None:
        self._idx = (self._idx + 1) % len(self._entries)
        self._consumed_this_round = 0


CURATED_SCENE_QUERIES: tuple[str, ...] = (
    "beach", "mountain", "forest", "sunset", "snow", "city street",
    "garden", "river", "lake", "bridge", "child", "family", "dog", "cat",
    "food", "flower", "concert", "wedding", "car", "boat", "sky",
    "portrait", "selfie", "architecture", "night", "rain", "tree",
)


class SceneSelector:
    """Themed slideshow driven by Immich's auto-discovered groupings.

    On first use, auto-detects what Immich exposes and picks the best source
    in this priority order:

        1. CLIP scene labels ('things' facet from /search/explore)
        2. Cities ('exifInfo.city' / 'city' facet from /search/explore)
        3. Named, non-hidden people (/people endpoint)
        4. Curated CLIP queries (hard-coded fallback that works whenever
           Immich's smart search is functional, even if /search/explore
           doesn't surface anything useful)

    Each rotation picks a random label from the chosen source and fetches a
    pool of matching assets — via CLIP smart search, city-filter metadata
    search, or person-filter metadata search, depending on the source.

    `current_scene` exposes the active label so the controller can publish
    it for HA / dashboard display.
    """

    SourceMode = Literal["things", "city", "curated"]

    def __init__(
        self,
        client: ImmichClient,
        *,
        pool_size: int = 25,
        force_mode: SourceMode | None = None,
    ) -> None:
        """`force_mode` skips auto-detect — useful for testing or if a user
        wants to pin behaviour."""
        self._client = client
        self._pool_size = pool_size
        self._force_mode = force_mode
        self._lock = threading.Lock()
        # Resolved on first call:
        self._mode: SceneSelector.SourceMode | None = None
        self._city_facet: str | None = None              # actual facet name in this Immich version
        # explore() result from _discover_mode, reused once by the immediately
        # following _collect_labels to avoid a duplicate round-trip. Cleared
        # after use so later label refills re-fetch fresh facets.
        self._explore_cache: dict[str, list[str]] | None = None
        # Per-rotation state:
        self._labels: list[str] = []
        self._current_scene: str | None = None
        self._pool: list[Asset] = []

    @property
    def current_scene(self) -> str | None:
        with self._lock:
            return self._current_scene

    @property
    def mode(self) -> "SceneSelector.SourceMode | None":
        with self._lock:
            return self._mode

    def next_batch(self, n: int) -> list[Asset]:
        with self._lock:
            if not self._pool:
                self._rotate()
            if not self._pool:
                return []
            take = min(n, len(self._pool))
            out = self._pool[:take]
            self._pool = self._pool[take:]
            return out

    # ── Mode discovery + rotation ───────────────────────────────────────
    def _rotate(self) -> None:
        if self._mode is None:
            self._mode = self._discover_mode()
            log.info("scene-mode source = %s", self._mode)

        if not self._labels:
            self._labels = self._collect_labels()
            if not self._labels:
                log.warning(
                    "scene-mode source %r has no labels available — "
                    "slideshow will hold until something changes upstream",
                    self._mode,
                )
                return

        self._current_scene = random.choice(self._labels)
        log.info("scene[%s] rotation -> %r", self._mode, self._current_scene)
        pool = self._query_assets(self._current_scene)
        random.shuffle(pool)
        self._pool = pool

    def _discover_mode(self) -> "SceneSelector.SourceMode":
        if self._force_mode is not None:
            return self._force_mode

        try:
            explore = self._client.explore()
        except ImmichError as e:
            log.warning("explore failed: %s — falling back to curated queries", e)
            return "curated"

        self._explore_cache = explore  # reused by the following _collect_labels

        if explore.get("things"):
            return "things"

        for k, v in explore.items():
            if k.endswith("city") and v:
                self._city_facet = k
                return "city"

        facets = sorted(explore.keys())
        log.warning(
            "no usable Immich classification available (explore facets: %s); "
            "falling back to curated CLIP queries. If smart search is enabled "
            "in your Immich, this still produces good variety. Run "
            "`immframe explore` for diagnostics.",
            facets or "none",
        )
        return "curated"

    def _collect_labels(self) -> list[str]:
        cached = self._explore_cache
        self._explore_cache = None
        if self._mode == "things":
            try:
                explore = cached if cached is not None else self._client.explore()
                return list(explore.get("things", []))
            except ImmichError:
                return []
        if self._mode == "city":
            try:
                explore = cached if cached is not None else self._client.explore()
            except ImmichError:
                return []
            if self._city_facet and explore.get(self._city_facet):
                return list(explore[self._city_facet])
            for k, v in explore.items():
                if k.endswith("city") and v:
                    self._city_facet = k
                    return list(v)
            return []
        if self._mode == "curated":
            return list(CURATED_SCENE_QUERIES)
        return []

    def _query_assets(self, label: str) -> list[Asset]:
        try:
            if self._mode in ("things", "curated"):
                return self._client.search_smart(label, count=self._pool_size)
            if self._mode == "city":
                return self._client.search_metadata(city=label, count=self._pool_size)
        except ImmichError as e:
            log.warning(
                "scene[%s] asset query for %r failed: %s",
                self._mode, label, e,
            )
        return []

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

from .client import ImmichClient, ImmichError, _to_asset, showable
from .models import Asset

log = logging.getLogger(__name__)


@runtime_checkable
class AssetSelector(Protocol):
    def next_batch(self, n: int) -> list[Asset]: ...


class RandomSelector:
    """Random via Immich `/search/random`, optionally narrowed.

    Immich already returns a random sample, so we just pass `n` through.
    The optional filters ride on the same request: `favorites` (starred
    assets only), `min_rating` (EXIF/Immich star rating >= N), `album_ids`,
    `tag_ids`. They compose — e.g. favourites within an album.
    """

    def __init__(
        self,
        client: ImmichClient,
        *,
        include_videos: bool = True,
        favorites: bool = False,
        min_rating: int | None = None,
        album_ids: list[str] | None = None,
        tag_ids: list[str] | None = None,
    ) -> None:
        self._client = client
        self._include_videos = include_videos
        self._favorites = bool(favorites)
        self._min_rating = int(min_rating) if min_rating is not None else None
        self._album_ids = list(album_ids) if album_ids else None
        self._tag_ids = list(tag_ids) if tag_ids else None

    @property
    def current_scene(self) -> str | None:
        if self._favorites:
            return "Favourites"
        if self._min_rating is not None:
            return f"Rated {self._min_rating}+"
        return None

    def next_batch(self, n: int) -> list[Asset]:
        kwargs: dict = {"with_video": self._include_videos}
        if self._favorites:
            kwargs["favorites"] = True
        if self._min_rating is not None:
            kwargs["min_rating"] = self._min_rating
        if self._album_ids:
            kwargs["album_ids"] = self._album_ids
        if self._tag_ids:
            kwargs["tag_ids"] = self._tag_ids
        try:
            return self._client.random_assets(n, **kwargs)
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
    next call. CLIP ranking is deterministic, so each call draws a random
    page from the top `pages` pages (page size = the batch size) — otherwise
    a query would show the same top-N photos forever.
    """

    def __init__(self, client: ImmichClient, query: str, *, pages: int = 4) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._query = query
        self._pages = max(1, int(pages))

    def set_query(self, query: str) -> None:
        with self._lock:
            self._query = query

    def next_batch(self, n: int) -> list[Asset]:
        with self._lock:
            q = self._query
        if not q:
            return []
        page = random.randint(1, self._pages)
        try:
            out = self._client.search_smart(q, count=n, page=page)
            if not out and page > 1:                     # fewer matches than pages
                out = self._client.search_smart(q, count=n, page=1)
            return out
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

    Uses `/search/random` with a `personIds` filter — server-side, and a
    fresh random sample per rotation (`/search/metadata` would hand back the
    same newest-first page every time). We never download the full library
    and filter client-side.

    `current_scene` exposes the currently-rotating person's NAME (not ID)
    so the controller surfaces it the same way as scene mode.
    """

    MAX_DRAWS = 8            # candidate draws per rotation before giving up

    def __init__(
        self,
        client: ImmichClient,
        person_ids: list[str] | None = None,
        *,
        pool_size: int = 25,
        min_photos: int = 0,
        favorites_only: bool = False,
    ) -> None:
        self._client = client
        self._explicit_ids = list(person_ids or [])
        self._pool_size = pool_size
        # Auto-rotation only considers people with at least this many
        # photos (counted via /search/statistics, lazily, cached) — a
        # "person of the moment" with 3 photos makes a poor slideshow.
        # Explicit `person_ids` are never filtered: the user asked for them.
        self._min_photos = max(0, int(min_photos))
        self._favorites_only = bool(favorites_only)
        self._lock = threading.Lock()
        # Lazily populated:
        self._person_index: dict[str, str] = {}      # id → name (for label exposure)
        self._rotation_ids: list[str] = []           # ids we cycle through
        self._photo_counts: dict[str, int] = {}      # id → asset count (min_photos check)
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
            # (photo counts stay cached — they don't depend on the selection)

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

        person_id = self._draw_person()
        if person_id is None:
            return
        self._current_name = self._person_index.get(person_id) or person_id
        log.info("people rotation -> %r (%s)", self._current_name, person_id)
        try:
            pool = self._client.random_assets(
                self._pool_size, person_ids=[person_id],
            )
        except ImmichError as e:
            log.warning("people random_assets for %s failed: %s", person_id, e)
            pool = []
        random.shuffle(pool)
        self._pool = pool

    def _draw_person(self) -> str | None:
        """Pick a person for this rotation. With `min_photos` set (and no
        explicit list) draw until someone with enough photos turns up; the
        counts are one cheap /search/statistics call each and cached, so
        the cost tails off to zero after a few rotations."""
        if self._explicit_ids or self._min_photos <= 0:
            return random.choice(self._rotation_ids)
        eligible = [pid for pid in self._rotation_ids
                    if self._photo_counts.get(pid, self._min_photos) >= self._min_photos]
        if not eligible:
            log.warning(
                "people mode: nobody has >= %d photos (people_min_photos) — "
                "lower the threshold or name more people in Immich",
                self._min_photos,
            )
            return None
        for _ in range(self.MAX_DRAWS):
            pid = random.choice(eligible)
            if pid not in self._photo_counts:
                try:
                    self._photo_counts[pid] = self._client.search_statistics(person_ids=[pid])
                except ImmichError as e:
                    log.debug("search_statistics(%s) failed: %s — assuming eligible", pid, e)
                    return pid
            n = self._photo_counts[pid]
            if n >= self._min_photos:
                return pid
            log.debug("people: %r has %d photos (< %d) — skipping",
                      self._person_index.get(pid, pid), n, self._min_photos)
            eligible = [p for p in eligible if p != pid]
            if not eligible:
                break
        # Everyone drawn so far was under the threshold; someone is better
        # than nobody — fall back to the largest known.
        known = [(self._photo_counts.get(p, 0), p) for p in self._rotation_ids]
        return max(known)[1] if known else None

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

        # No explicit list: rotate every named, non-hidden person (or only
        # the ones starred as favourites in Immich when favorites_only).
        try:
            people = self._client.list_people()
        except ImmichError as e:
            log.warning("list_people failed: %s", e)
            return []
        named = [p for p in people if p.get("name") and not p.get("isHidden") and p.get("id")]
        if self._favorites_only:
            starred = [p for p in named if p.get("isFavorite")]
            if starred:
                named = starred
            else:
                log.warning(
                    "people_favorites_only is set but no person is starred in "
                    "Immich — rotating through all %d named people", len(named),
                )
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
        pool = [_to_asset(a) for a in asset_dicts if showable(a)]
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
    `days` window. Each rotation draws a fresh random sample of up to
    `pool_size` assets from that window (via `/search/random`, so it is a
    different sample each time), shuffles it and serves it without
    replacement; the window is re-queried when the pool runs dry, so newly
    uploaded photos surface quickly.

    Small windows: when the sample comes back short (fewer than `pool_size`
    — i.e. it *is* the whole window), the selector returns `[]` once after
    serving it. In a playlist that advances to the next entry instead of
    replaying the same few photos until the entry's count is met; standalone
    the worker just backs off briefly and a new rotation begins.

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
        self._lock = threading.Lock()
        self._pool: list[Asset] = []
        self._pool_is_window = False    # last sample was short: it's the whole window
        # True once a whole-window sample has been fully served; the next
        # call yields [] and clears it.
        self._window_done = False

    @property
    def current_scene(self) -> str | None:
        return f"Last {self._days} days"

    def next_batch(self, n: int) -> list[Asset]:
        with self._lock:
            if not self._pool:
                if self._window_done:
                    self._window_done = False
                    return []
                self._refill()
                if not self._pool:
                    return []
            take = min(n, len(self._pool))
            out = self._pool[:take]
            self._pool = self._pool[take:]
            if not self._pool and self._pool_is_window:
                self._window_done = True
            return out

    def _refill(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._days)
        try:
            if self._field == "taken":
                assets = self._client.random_assets(self._pool_size, taken_after=cutoff)
            else:
                assets = self._client.random_assets(self._pool_size, created_after=cutoff)
        except ImmichError as e:
            log.warning("recent random_assets failed: %s", e)
            assets = []
        random.shuffle(assets)
        self._pool = assets
        # A short sample means the window has fewer assets than pool_size —
        # we're holding all of it.
        self._pool_is_window = bool(assets) and len(assets) < self._pool_size


class PlaylistSelector:
    """Round-robins through a sequence of (selector, count[, is_collage]) entries.

    On each `next_batch(n)` call, draws up to `n` assets from the current
    entry's selector, counting toward its `count` quota. When the quota
    fills or the sub-selector returns nothing, advances to the next entry.
    Cycles indefinitely.

    Useful for "show 25 random, then 25 on-this-day, then 25 of Alice,
    repeat" without picking just one mode.

    Per-entry collage: an entry may carry `is_collage=True`. The prefetch
    worker checks `collage_active()` before drawing a batch and, when true,
    composites that batch into a single collage. For collage entries `count`
    means *number of collages* (each `next_batch` call is one collage), so a
    single playlist can interleave full-screen photos and collages from the
    same source.
    """

    def __init__(
        self, entries: list[tuple],
    ) -> None:
        if not entries:
            raise ValueError("PlaylistSelector requires at least one entry")
        self._entries = [self._normalise(e) for e in entries]
        self._lock = threading.Lock()
        self._idx = 0
        self._consumed_this_round = 0

    @staticmethod
    def _normalise(entry: tuple):
        """Accept legacy `(selector, count)` or `(selector, count, collage)`,
        where `collage` is a CollageConfig (collage entry) or None (singles)."""
        if len(entry) == 3:
            sel, cnt, col = entry
            return (sel, int(cnt), col)
        sel, cnt = entry
        return (sel, int(cnt), None)

    @property
    def current_scene(self) -> str | None:
        with self._lock:
            sel = self._entries[self._idx][0]
        # Expose the inner selector's label if it has one
        return getattr(sel, "current_scene", None)

    def current_collage(self):
        """The current entry's collage config (a CollageConfig) when it's a
        collage entry, else None. The prefetch worker composites the batch
        using this config (per-entry overrides included)."""
        with self._lock:
            return self._entries[self._idx][2]

    def collage_active(self) -> bool:
        """True when the current entry is a collage entry."""
        with self._lock:
            return self._entries[self._idx][2] is not None

    def next_batch(self, n: int) -> list[Asset]:
        with self._lock:
            # Try every entry once before giving up — covers the case where
            # the first few are empty (recent with no new uploads, etc.)
            for _ in range(len(self._entries)):
                sel, cnt, col = self._entries[self._idx]
                is_collage = col is not None
                remaining = max(0, cnt - self._consumed_this_round)
                if remaining == 0:
                    self._advance()
                    continue
                if is_collage:
                    # One call == one collage; `n` is the worker's tile count.
                    batch = sel.next_batch(n)
                    if not batch:
                        self._advance()
                        continue
                    self._consumed_this_round += 1          # count collages
                else:
                    take = min(n, remaining)
                    batch = sel.next_batch(take)
                    if not batch:
                        self._advance()
                        continue
                    self._consumed_this_round += len(batch)  # count photos
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
        2. Cities — the full list from /search/cities, falling back to the
           'exifInfo.city' / 'city' facet of /search/explore (which Immich
           caps at 12 alphabetically-first entries, so on its own a large
           library only ever rotates through its "A" cities)
        3. Curated CLIP queries (hard-coded fallback that works whenever
           Immich's smart search is functional, even if Immich surfaces
           nothing useful)

    A label whose pool comes back with fewer than `MIN_POOL` assets is
    skipped for another (up to `MAX_LABEL_TRIES` draws) — a one-photo city
    makes neither a scene nor a collage.

    Each rotation picks a random label from the chosen source and fetches a
    pool of matching assets — via CLIP smart search (things / curated) or a
    city-filtered random sample (city), depending on the source.

    `current_scene` exposes the active label so the controller can publish
    it for HA / dashboard display.
    """

    SourceMode = Literal["things", "city", "curated"]
    MIN_POOL = 3
    MAX_LABEL_TRIES = 4

    def __init__(
        self,
        client: ImmichClient,
        *,
        pool_size: int = 25,
        force_mode: SourceMode | None = None,
        pages: int = 4,
    ) -> None:
        """`force_mode` skips auto-detect — `selection.scene_source` in
        config (or `source:` on a playlist entry) lands here. `pages`: for
        the CLIP-backed sources (things / curated), each rotation fetches a
        random page from the top `pages` pages of the ranked results so a
        label doesn't always produce the same 25 photos."""
        self._client = client
        self._pool_size = pool_size
        self._force_mode = force_mode
        self._pages = max(1, int(pages))
        self._lock = threading.Lock()
        # Resolved on first call:
        self._mode: SceneSelector.SourceMode | None = None
        self._city_facet: str | None = None              # actual facet name in this Immich version
        # explore() result from _discover_mode, reused once by the immediately
        # following _collect_labels to avoid a duplicate round-trip. Cleared
        # after use so later label refills re-fetch fresh facets.
        self._explore_cache: dict[str, list[str]] | None = None
        self._cities_cache: list[str] | None = None      # same idea, for /search/cities
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

        label = random.choice(self._labels)
        pool = self._query_assets(label)
        tries = 1
        while len(pool) < self.MIN_POOL and tries < self.MAX_LABEL_TRIES and len(self._labels) > 1:
            log.debug("scene[%s] %r has %d asset(s) — trying another label",
                      self._mode, label, len(pool))
            label = random.choice(self._labels)
            pool = self._query_assets(label)
            tries += 1
        self._current_scene = label
        log.info("scene[%s] rotation -> %r (%d assets)", self._mode, label, len(pool))
        random.shuffle(pool)
        self._pool = pool

    def _discover_mode(self) -> "SceneSelector.SourceMode":
        if self._force_mode is not None:
            return self._force_mode

        try:
            explore = self._client.explore()
        except ImmichError as e:
            log.warning("explore failed: %s", e)
            explore = {}

        if explore.get("things"):
            self._explore_cache = explore  # reused by the following _collect_labels
            return "things"

        cities = self._fetch_cities()
        if cities:
            self._cities_cache = cities
            return "city"

        for k, v in explore.items():
            if k.endswith("city") and v:
                self._city_facet = k
                self._explore_cache = explore
                return "city"

        facets = sorted(explore.keys())
        log.warning(
            "no usable Immich classification available (explore facets: %s, "
            "no cities); falling back to curated CLIP queries. If smart search "
            "is enabled in your Immich, this still produces good variety. Run "
            "`immframe explore` for diagnostics.",
            facets or "none",
        )
        return "curated"

    def _fetch_cities(self) -> list[str]:
        try:
            return self._client.list_cities()
        except ImmichError as e:
            log.warning("list_cities failed: %s — trying explore's city facet", e)
            return []

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
            cities = self._cities_cache
            self._cities_cache = None
            if cities is None:
                cities = self._fetch_cities()
            if cities:
                return list(cities)
            # Older Immich without /search/cities: explore's (capped) facet.
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
                page = random.randint(1, self._pages)
                out = self._client.search_smart(label, count=self._pool_size, page=page)
                if not out and page > 1:                 # fewer matches than pages
                    out = self._client.search_smart(label, count=self._pool_size, page=1)
                return out
            if self._mode == "city":
                # Random sample, not /search/metadata: that returns the same
                # newest-first page for a city every rotation.
                return self._client.random_assets(self._pool_size, city=label)
        except ImmichError as e:
            log.warning(
                "scene[%s] asset query for %r failed: %s",
                self._mode, label, e,
            )
        return []

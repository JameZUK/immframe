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
from typing import Protocol, runtime_checkable

from .client import ImmichClient, ImmichError
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


class SceneSelector:
    """Themed slideshow driven by Immich's CLIP scene classification.

    Workflow:
        1. Query /search/explore to discover scene labels ("beach",
           "mountain", "forest", ...). The `field_name` arg picks which
           facet — default "things" is CLIP scenes; "people" surfaces
           named faces.
        2. Pick a random scene from that list.
        3. Use smart search to fetch a pool of assets matching that scene.
        4. Iterate the pool until empty, then rotate to a fresh scene.

    The current scene name is exposed via `current_scene` so the controller
    can publish it.

    Network failures (Explore unavailable, smart search returns nothing)
    surface as empty batches; the prefetch worker backs off.
    """

    DEFAULT_FIELD = "things"

    def __init__(
        self,
        client: ImmichClient,
        *,
        field_name: str = DEFAULT_FIELD,
        pool_size: int = 25,
    ) -> None:
        self._client = client
        self._field_name = field_name
        self._pool_size = pool_size
        self._lock = threading.Lock()
        self._scenes: list[str] = []
        self._current_scene: str | None = None
        self._pool: list[Asset] = []

    @property
    def current_scene(self) -> str | None:
        with self._lock:
            return self._current_scene

    def next_batch(self, n: int) -> list[Asset]:
        with self._lock:
            if not self._pool:
                self._rotate_scene()
            if not self._pool:
                return []
            take = min(n, len(self._pool))
            out = self._pool[:take]
            self._pool = self._pool[take:]
            return out

    def _rotate_scene(self) -> None:
        if not self._scenes:
            try:
                explore = self._client.explore()
            except ImmichError as e:
                log.warning("explore failed: %s", e)
                return
            self._scenes = list(explore.get(self._field_name, []))
            if not self._scenes:
                log.info(
                    "explore returned no %r facet — has Immich finished "
                    "classifying the library?", self._field_name,
                )
                return

        self._current_scene = random.choice(self._scenes)
        log.info("scene rotation -> %r", self._current_scene)
        try:
            pool = self._client.search_smart(self._current_scene, count=self._pool_size)
        except ImmichError as e:
            log.warning("scene smart-search(%r) failed: %s", self._current_scene, e)
            pool = []
        random.shuffle(pool)
        self._pool = pool

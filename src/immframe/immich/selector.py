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

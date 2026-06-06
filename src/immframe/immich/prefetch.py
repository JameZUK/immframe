"""Background worker that keeps a queue of ready-to-show assets.

Pipeline:
    selector.next_batch(...) -> [Asset]
    for each asset:
        if image:  download preview to a temp file → queue (path, asset, ocr)
        if video:  queue (path|None, asset, None) — MPV streams the clip
                   directly from Immich; `path` is the matted-poster JPEG

Queue items are `(path, asset, ocr)` triples. `ocr` is the list of OCR
strings for the asset (fetched here, OFF the render thread) when the caller
asked for it via `wants_ocr`, else `None`. Doing the OCR round-trip in this
worker keeps the controller's render loop from blocking on the network.

Ownership: after `next()` returns `(path, asset, ocr)`, the CALLER owns
`path` and must `unlink(missing_ok=True)` it once the slide is done. `path`
is `None` for videos whose poster download failed. The worker only deletes
files that are still in the queue when `drain()`/`stop()` is called.

`drain()` empties the queue without stopping the worker — call when swapping
selector or filters so stale assets don't surface.
"""
from __future__ import annotations

import logging
import queue
import random
import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .client import ImmichClient, ImmichError
from .models import Asset, AssetKind
from .selector import AssetSelector

if TYPE_CHECKING:
    from ..config import CollageConfig

log = logging.getLogger(__name__)


QueueItem = tuple[Path | None, Asset, list[str] | None]


class PrefetchWorker:
    def __init__(
        self,
        selector: AssetSelector,
        client: ImmichClient,
        *,
        queue_size: int = 5,
        empty_backoff_s: float = 5.0,
        wants_ocr: Callable[[], bool] | None = None,
        collage: "CollageConfig | None" = None,
        collage_label: Callable[[int], str] | None = None,
    ) -> None:
        self._client = client
        # Predicate, re-read per fetch so a runtime show_text toggle is honored.
        self._wants_ocr = wants_ocr or (lambda: False)
        # When set, each queue item is a single composited collage instead of
        # one asset. Canvas defaults until the controller learns the display
        # size (set_collage_canvas) at start().
        self._collage = collage
        self._collage_label = collage_label
        self._collage_canvas: tuple[int, int] = (1920, 1080)
        self._seq = 0                                   # unique temp-file suffix
        self._selector_lock = threading.Lock()
        self._selector = selector
        self._gen = 0                                   # bumped on set_selector / drain
        self._queue: queue.Queue[QueueItem] = queue.Queue(maxsize=queue_size)
        self._stop_evt = threading.Event()
        self._empty_backoff_s = empty_backoff_s
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="immframe-"))
        self._thread: threading.Thread | None = None

    def set_collage_canvas(self, w: int, h: int) -> None:
        """Composite collages at the real display resolution (set by the
        controller once pi3d reports its size)."""
        if w and h and w > 0 and h > 0:
            self._collage_canvas = (int(w), int(h))

    # ── Lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="prefetch", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
            self._thread = None
        self._drain_queue()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    # ── Consumer API ────────────────────────────────────────────────────
    def next(self, *, timeout: float | None = None) -> QueueItem | None:
        if self._stop_evt.is_set():
            return None
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ── Control ─────────────────────────────────────────────────────────
    def drain(self) -> None:
        with self._selector_lock:
            self._gen += 1
        self._drain_queue()

    def set_selector(self, selector: AssetSelector) -> None:
        with self._selector_lock:
            self._selector = selector
            self._gen += 1
        self._drain_queue()

    # ── Internals ───────────────────────────────────────────────────────
    def _drain_queue(self) -> None:
        while True:
            try:
                path = self._queue.get_nowait()[0]
            except queue.Empty:
                return
            if path is not None:
                path.unlink(missing_ok=True)

    def _snapshot(self) -> tuple[AssetSelector, int]:
        with self._selector_lock:
            return self._selector, self._gen

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            selector, gen = self._snapshot()

            if self._collage is not None:
                item = self._fetch_collage(selector)
                if item is None:
                    self._stop_evt.wait(self._empty_backoff_s)
                    continue
                _, gen_now = self._snapshot()
                if gen_now != gen:                      # selector swapped mid-compose
                    self._discard(item)
                    continue
                if not self._enqueue(item):
                    return
                continue

            try:
                batch = selector.next_batch(self._queue.maxsize)
            except Exception as e:
                log.exception("selector.next_batch raised: %s", e)
                batch = []

            if not batch:
                self._stop_evt.wait(self._empty_backoff_s)
                continue

            for asset in batch:
                if self._stop_evt.is_set():
                    return
                item = self._fetch(asset)
                if item is None:
                    continue

                # Discard if selector was swapped while we were downloading.
                _, gen_now = self._snapshot()
                if gen_now != gen:
                    path = item[0]
                    if path is not None:
                        path.unlink(missing_ok=True)
                    break

                if not self._enqueue(item):
                    return

    def _fetch(self, asset: Asset) -> QueueItem | None:
        if asset.kind == AssetKind.IMAGE:
            dest = self._tmp_dir / f"{asset.id}.jpg"
            try:
                self._client.download_preview(asset.id, dest)
            except ImmichError as e:
                log.warning("download_preview(%s) failed: %s", asset.id, e)
                return None
            return (dest, asset, self._fetch_ocr(asset))
        if asset.kind == AssetKind.VIDEO:
            # Also fetch a still poster JPEG for videos — the controller
            # renders it via pi3d (matted, faded-in) before MPV takes over
            # for playback. If the poster download fails we still play the
            # video, just without the matted pre-roll.
            dest = self._tmp_dir / f"{asset.id}.poster.jpg"
            try:
                self._client.download_preview(asset.id, dest)
                return (dest, asset, None)
            except ImmichError as e:
                log.warning(
                    "video poster download(%s) failed: %s — playing without poster",
                    asset.id, e,
                )
                return (None, asset, None)
        return None                                     # OTHER/AUDIO: skip

    # ── Collage ─────────────────────────────────────────────────────────
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _fetch_collage(self, selector: AssetSelector) -> QueueItem | None:
        """Pull a random K assets, download each as a still, composite them
        into one JPEG, and return a single (path, synthetic_asset, None) item.
        Source stills are deleted once composited (only the collage is kept)."""
        cfg = self._collage
        assert cfg is not None
        k = random.randint(cfg.min_tiles, cfg.max_tiles)
        try:
            assets = selector.next_batch(k)
        except Exception as e:
            log.exception("selector.next_batch raised: %s", e)
            return None
        if not assets:
            return None

        sources: list[tuple[Path, Asset]] = []
        for asset in assets:
            if self._stop_evt.is_set():
                self._cleanup_sources(sources)
                return None
            if asset.kind == AssetKind.OTHER:           # audio/other: not a tile
                continue
            # Images and videos both contribute a still (video → poster frame).
            src = self._tmp_dir / f"src-{asset.id}-{self._next_seq()}.jpg"
            try:
                self._client.download_preview(asset.id, src)
            except ImmichError as e:
                log.debug("collage source download(%s) failed: %s", asset.id, e)
                continue
            sources.append((src, asset))

        if len(sources) < 2:                            # not enough for a collage
            self._cleanup_sources(sources)
            return None

        from ..collage import render_collage, make_collage_asset
        stem = f"collage-{self._next_seq()}"
        dest = self._tmp_dir / f"{stem}.jpg"
        ok = render_collage(
            [p for p, _ in sources],
            [a.is_portrait for _, a in sources],
            dest,
            canvas_size=self._collage_canvas,
            gap=cfg.gap,
            background=cfg.background,
            fit=cfg.fit,
            layout=cfg.layout,
        )
        self._cleanup_sources(sources)                  # composite is self-contained
        if not ok:
            dest.unlink(missing_ok=True)
            return None

        count = len(sources)
        label = self._collage_label(count) if self._collage_label else f"{count} photos"
        return (dest, make_collage_asset(stem, label, count), None)

    def _cleanup_sources(self, sources: list[tuple[Path, Asset]]) -> None:
        for path, _ in sources:
            path.unlink(missing_ok=True)

    def _discard(self, item: QueueItem) -> None:
        path = item[0]
        if path is not None:
            path.unlink(missing_ok=True)

    def _fetch_ocr(self, asset: Asset) -> list[str] | None:
        """Fetch OCR text for an image when the consumer wants it. Done here,
        on the worker thread, so the render loop never blocks on this network
        round-trip. Returns None when not wanted or on failure."""
        if not self._wants_ocr():
            return None
        try:
            return self._client.get_ocr(asset.id)
        except ImmichError as e:
            log.debug("get_ocr(%s) failed: %s", asset.id, e)
            return None

    def _enqueue(self, item: QueueItem) -> bool:
        """Block on a full queue; return False if shutdown was signalled."""
        while not self._stop_evt.is_set():
            try:
                self._queue.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        path = item[0]
        if path is not None:
            path.unlink(missing_ok=True)
        return False

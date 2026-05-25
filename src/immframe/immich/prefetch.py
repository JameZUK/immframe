"""Background worker that keeps a queue of ready-to-show assets.

Pipeline:
    selector.next_batch(...) -> [Asset]
    for each asset:
        if image:  download preview to a temp file → queue (path, asset)
        if video:  queue (None, asset) — streamed by MPV directly from Immich

Ownership: after `next()` returns `(path, asset)`, the CALLER owns `path`
and must `unlink(missing_ok=True)` it once the slide is done. `path` is
`None` for videos (no temp file). The worker only deletes files that are
still in the queue when `drain()`/`stop()` is called.

`drain()` empties the queue without stopping the worker — call when swapping
selector or filters so stale assets don't surface.
"""
from __future__ import annotations

import logging
import queue
import shutil
import tempfile
import threading
from pathlib import Path

from .client import ImmichClient, ImmichError
from .models import Asset, AssetKind
from .selector import AssetSelector

log = logging.getLogger(__name__)


QueueItem = tuple[Path | None, Asset]


class PrefetchWorker:
    def __init__(
        self,
        selector: AssetSelector,
        client: ImmichClient,
        *,
        queue_size: int = 5,
        empty_backoff_s: float = 5.0,
    ) -> None:
        self._client = client
        self._selector_lock = threading.Lock()
        self._selector = selector
        self._gen = 0                                   # bumped on set_selector / drain
        self._queue: queue.Queue[QueueItem] = queue.Queue(maxsize=queue_size)
        self._stop_evt = threading.Event()
        self._empty_backoff_s = empty_backoff_s
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="immframe-"))
        self._thread: threading.Thread | None = None

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
                path, _ = self._queue.get_nowait()
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
                    path, _ = item
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
            return (dest, asset)
        if asset.kind == AssetKind.VIDEO:
            # Also fetch a still poster JPEG for videos — the controller
            # renders it via pi3d (matted, faded-in) before MPV takes over
            # for playback. If the poster download fails we still play the
            # video, just without the matted pre-roll.
            dest = self._tmp_dir / f"{asset.id}.poster.jpg"
            try:
                self._client.download_preview(asset.id, dest)
                return (dest, asset)
            except ImmichError as e:
                log.warning(
                    "video poster download(%s) failed: %s — playing without poster",
                    asset.id, e,
                )
                return (None, asset)
        return None                                     # OTHER/AUDIO: skip

    def _enqueue(self, item: QueueItem) -> bool:
        """Block on a full queue; return False if shutdown was signalled."""
        while not self._stop_evt.is_set():
            try:
                self._queue.put(item, timeout=0.5)
                return True
            except queue.Full:
                continue
        path, _ = item
        if path is not None:
            path.unlink(missing_ok=True)
        return False

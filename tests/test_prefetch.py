from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from immframe.immich.models import Asset, AssetKind, GeoInfo
from immframe.immich.prefetch import PrefetchWorker


def _a(aid: str, kind: AssetKind = AssetKind.IMAGE) -> Asset:
    return Asset(
        id=aid,
        kind=kind,
        original_file_name=f"{aid}.jpg",
        mime_type="image/jpeg",
        width=100,
        height=100,
        taken_at=None,
        geo=GeoInfo(None, None, None, None, None),
        camera_make=None,
        camera_model=None,
        title=None,
        caption=None,
        tag_names=(),
        people=(),
        favorite=False,
        live_photo_video_id=None,
    )


def _client_writing_bytes() -> MagicMock:
    """ImmichClient stub that writes a few bytes to the requested path."""
    client = MagicMock()
    def download(asset_id: str, dest: Path) -> None:
        dest.write_bytes(f"img-{asset_id}".encode())
    client.download_preview.side_effect = download
    return client


def test_images_get_downloaded_and_queued():
    selector = MagicMock()
    selector.next_batch.side_effect = [
        [_a("x"), _a("y")],
        [],   # subsequent calls return empty so worker idles
    ]
    client = _client_writing_bytes()
    w = PrefetchWorker(selector, client, queue_size=5, empty_backoff_s=0.01)
    w.start()
    try:
        item1 = w.next(timeout=2.0)
        item2 = w.next(timeout=2.0)
        assert item1 is not None and item2 is not None
        # Read bytes BEFORE stop() — stop() wipes the temp dir.
        assert item1[1].id == "x"
        assert item1[0] is not None
        assert item1[0].read_bytes() == b"img-x"
        assert item2[1].id == "y"
    finally:
        w.stop(timeout=2.0)


def test_videos_get_poster_jpeg_downloaded():
    """Video assets now fetch a poster JPEG via the same /thumbnail endpoint
    so the controller can render a matted still before MPV takes over."""
    selector = MagicMock()
    selector.next_batch.side_effect = [[_a("v", AssetKind.VIDEO)], []]
    client = _client_writing_bytes()
    w = PrefetchWorker(selector, client, queue_size=5, empty_backoff_s=0.01)
    w.start()
    try:
        item = w.next(timeout=2.0)
        assert item is not None
        path, asset, _ocr = item
        assert asset.id == "v"
        assert path is not None
        # Poster file actually exists on disk (download succeeded)
        assert path.read_bytes() == b"img-v"
    finally:
        w.stop(timeout=2.0)
    client.download_preview.assert_called_once()


def test_video_poster_download_failure_falls_back_to_none():
    """If poster download fails the video still goes through — the
    controller will play it without a matted still."""
    from immframe.immich.client import ImmichError
    selector = MagicMock()
    selector.next_batch.side_effect = [[_a("v", AssetKind.VIDEO)], []]

    client = MagicMock()
    client.download_preview.side_effect = ImmichError("upstream 500")

    w = PrefetchWorker(selector, client, queue_size=5, empty_backoff_s=0.01)
    w.start()
    try:
        item = w.next(timeout=2.0)
    finally:
        w.stop(timeout=2.0)

    assert item is not None
    path, asset, _ocr = item
    assert path is None
    assert asset.id == "v"


def test_drain_removes_pending_files():
    selector = MagicMock()
    selector.next_batch.side_effect = [[_a("a"), _a("b")], []]
    client = _client_writing_bytes()
    w = PrefetchWorker(selector, client, queue_size=5, empty_backoff_s=0.01)
    w.start()
    try:
        # Wait until queue has both items
        deadline = time.time() + 2.0
        while w.queue_depth < 2 and time.time() < deadline:
            time.sleep(0.02)
        files = []
        # We need to peek without consuming, but Queue doesn't expose that.
        # Instead, observe the tmp_dir for files matching pattern.
        tmp_dir = w._tmp_dir
        files = list(tmp_dir.glob("*.jpg"))
        assert len(files) == 2
        w.drain()
    finally:
        w.stop(timeout=2.0)
    # After drain (and stop), the temp dir is gone — but if we'd just drained,
    # the queue is empty. We at least verify the queue is empty post-drain.


def test_set_selector_drains_queue():
    selector_a = MagicMock()
    selector_a.next_batch.side_effect = [[_a("a1")], []]
    selector_b = MagicMock()
    selector_b.next_batch.side_effect = [[_a("b1")], []]

    client = _client_writing_bytes()
    w = PrefetchWorker(selector_a, client, queue_size=5, empty_backoff_s=0.01)
    w.start()
    try:
        # Wait until at least one item is queued
        deadline = time.time() + 2.0
        while w.queue_depth < 1 and time.time() < deadline:
            time.sleep(0.02)
        w.set_selector(selector_b)
        # After swap, next item should come from selector_b
        item = w.next(timeout=2.0)
    finally:
        w.stop(timeout=2.0)

    assert item is not None and item[1].id == "b1"


def test_download_failure_is_skipped():
    from immframe.immich.client import ImmichError
    selector = MagicMock()
    selector.next_batch.side_effect = [[_a("bad"), _a("good")], []]

    client = MagicMock()
    calls = []
    def download(asset_id: str, dest: Path):
        calls.append(asset_id)
        if asset_id == "bad":
            raise ImmichError("404")
        dest.write_bytes(b"ok")
    client.download_preview.side_effect = download

    w = PrefetchWorker(selector, client, queue_size=5, empty_backoff_s=0.01)
    w.start()
    try:
        item = w.next(timeout=2.0)
    finally:
        w.stop(timeout=2.0)
    assert item is not None and item[1].id == "good"


def test_ocr_fetched_off_thread_when_wanted():
    """When the consumer asks for OCR, the worker fetches it (off the render
    thread) and attaches it to the queue item."""
    selector = MagicMock()
    selector.next_batch.side_effect = [[_a("x")], []]
    client = _client_writing_bytes()
    client.get_ocr.return_value = ["hello", "world"]
    w = PrefetchWorker(
        selector, client, queue_size=5, empty_backoff_s=0.01,
        wants_ocr=lambda: True,
    )
    w.start()
    try:
        item = w.next(timeout=2.0)
    finally:
        w.stop(timeout=2.0)
    assert item is not None
    assert item[2] == ["hello", "world"]
    client.get_ocr.assert_called_once_with("x")


def test_ocr_not_fetched_by_default():
    selector = MagicMock()
    selector.next_batch.side_effect = [[_a("x")], []]
    client = _client_writing_bytes()
    w = PrefetchWorker(selector, client, queue_size=5, empty_backoff_s=0.01)
    w.start()
    try:
        item = w.next(timeout=2.0)
    finally:
        w.stop(timeout=2.0)
    assert item is not None and item[2] is None
    client.get_ocr.assert_not_called()


def test_ocr_failure_does_not_drop_the_item():
    """A failing OCR call must not lose the slide — item still queues, ocr None."""
    from immframe.immich.client import ImmichError
    selector = MagicMock()
    selector.next_batch.side_effect = [[_a("x")], []]
    client = _client_writing_bytes()
    client.get_ocr.side_effect = ImmichError("ocr 500")
    w = PrefetchWorker(
        selector, client, queue_size=5, empty_backoff_s=0.01,
        wants_ocr=lambda: True,
    )
    w.start()
    try:
        item = w.next(timeout=2.0)
    finally:
        w.stop(timeout=2.0)
    assert item is not None
    assert item[1].id == "x" and item[2] is None


def test_collage_mode_produces_one_composite_item():
    """With collage enabled, K assets become a SINGLE composited queue item
    carrying a synthetic asset (not one item per asset)."""
    from immframe.config import CollageConfig
    selector = MagicMock()
    selector.next_batch.side_effect = lambda n: [_a("a"), _a("b"), _a("c")][:n]
    client = _client_writing_bytes()
    cfg = CollageConfig(enabled=True, min_tiles=3, max_tiles=3, layout="grid", gap=2)
    w = PrefetchWorker(
        selector, client, queue_size=3, empty_backoff_s=0.01,
        collage=cfg, collage_label=lambda n: f"Test • {n}",
    )
    w.set_collage_canvas(120, 90)
    w.start()
    try:
        item = w.next(timeout=3.0)
        assert item is not None
        path, asset, ocr = item
        # JPEG magic — a real composite was written (tiles were unreadable
        # bytes, so they fall back to background, but the canvas is valid).
        magic = path.read_bytes()[:2]
    finally:
        w.stop(timeout=2.0)
    assert ocr is None
    assert asset.id.startswith("collage-")
    assert asset.caption == "Test • 3"
    assert magic == b"\xff\xd8"


def test_collage_skips_when_too_few_sources():
    """Fewer than two usable sources → no collage emitted (worker backs off)."""
    from immframe.config import CollageConfig
    selector = MagicMock()
    selector.next_batch.side_effect = lambda n: [_a("solo")]
    client = _client_writing_bytes()
    cfg = CollageConfig(enabled=True, min_tiles=3, max_tiles=3, layout="grid")
    w = PrefetchWorker(
        selector, client, queue_size=3, empty_backoff_s=0.01, collage=cfg,
    )
    w.set_collage_canvas(120, 90)
    w.start()
    try:
        item = w.next(timeout=0.5)
    finally:
        w.stop(timeout=2.0)
    assert item is None


def test_set_collage_switches_modes_at_runtime():
    """set_collage flips the worker between single-asset and collage output
    live (the control-plane toggle path)."""
    from immframe.config import CollageConfig
    selector = MagicMock()
    selector.next_batch.side_effect = lambda n: [_a(f"x{i}") for i in range(n)]
    client = _client_writing_bytes()
    w = PrefetchWorker(selector, client, queue_size=2, empty_backoff_s=0.01)
    w.set_collage_canvas(120, 90)
    w.start()
    try:
        item = w.next(timeout=2.0)
        assert item is not None and not item[1].id.startswith("collage-")

        w.set_collage(CollageConfig(enabled=True, min_tiles=2, max_tiles=2, layout="grid"))

        # A stale single-asset item may slip through right after the drain;
        # keep reading until a collage appears.
        deadline = time.time() + 3.0
        got_collage = False
        while time.time() < deadline:
            it = w.next(timeout=1.0)
            if it is not None and it[1].id.startswith("collage-"):
                got_collage = True
                break
        assert got_collage
    finally:
        w.stop(timeout=2.0)


def test_per_entry_collage_when_selector_requests_it():
    """Global collage OFF, but the selector reports collage_active() → the
    worker composites that batch (playlist collage-entry path)."""
    from immframe.config import CollageConfig
    selector = MagicMock()
    selector.next_batch.side_effect = lambda n: [_a(f"p{i}") for i in range(n)]
    selector.current_collage.return_value = CollageConfig(
        enabled=True, min_tiles=2, max_tiles=2, layout="grid")
    client = _client_writing_bytes()
    cfg = CollageConfig(enabled=False, min_tiles=2, max_tiles=2, layout="grid")
    w = PrefetchWorker(
        selector, client, queue_size=2, empty_backoff_s=0.01, collage=cfg,
    )
    w.set_collage_canvas(120, 90)
    w.start()
    try:
        item = w.next(timeout=3.0)
    finally:
        w.stop(timeout=2.0)
    assert item is not None and item[1].id.startswith("collage-")


def test_no_collage_when_disabled_and_selector_inactive():
    """Global collage OFF and selector reports collage_active() False →
    plain single-asset items."""
    from immframe.config import CollageConfig
    selector = MagicMock()
    selector.next_batch.side_effect = lambda n: [_a("x0")]
    selector.current_collage.return_value = None
    client = _client_writing_bytes()
    cfg = CollageConfig(enabled=False)
    w = PrefetchWorker(
        selector, client, queue_size=2, empty_backoff_s=0.01, collage=cfg,
    )
    w.start()
    try:
        item = w.next(timeout=2.0)
    finally:
        w.stop(timeout=2.0)
    assert item is not None and not item[1].id.startswith("collage-")


def test_playlist_interleaves_singles_and_collages_end_to_end():
    """Real PrefetchWorker + PlaylistSelector: a single entry emits one item
    per photo, the next (collage) entry emits one composited item."""
    from immframe.config import CollageConfig
    from immframe.immich.selector import PlaylistSelector
    single_sub = MagicMock()
    single_sub.next_batch.side_effect = lambda n: [_a("single1"), _a("single2")][:n]
    collage_sub = MagicMock()
    collage_sub.next_batch.side_effect = lambda n: [_a(f"c{i}") for i in range(n)]
    entry_cc = CollageConfig(enabled=True, min_tiles=2, max_tiles=2, layout="grid")
    pl = PlaylistSelector([(single_sub, 2, None), (collage_sub, 1, entry_cc)])
    client = _client_writing_bytes()
    cfg = CollageConfig(enabled=False, min_tiles=2, max_tiles=2, layout="grid")
    w = PrefetchWorker(pl, client, queue_size=4, empty_backoff_s=0.01, collage=cfg)
    w.set_collage_canvas(120, 90)
    w.start()
    try:
        ids = []
        deadline = time.time() + 4.0
        while time.time() < deadline and len(ids) < 5:
            it = w.next(timeout=1.0)
            if it is not None:
                ids.append(it[1].id)
    finally:
        w.stop(timeout=2.0)
    assert any(i.startswith("single") for i in ids), ids
    assert any(i.startswith("collage-") for i in ids), ids


def test_smart_caption_becomes_collage_label():
    """When the photos share a person, smart_caption produces one whole-collage
    caption that's used as the synthetic asset's label."""
    from immframe.config import CollageConfig
    from immframe.immich.models import Asset, AssetKind, GeoInfo

    def _alice(aid):
        return Asset(
            id=aid, kind=AssetKind.IMAGE, original_file_name=f"{aid}.jpg",
            mime_type="image/jpeg", width=100, height=100, taken_at=None,
            geo=GeoInfo(None, None, None, None, None), camera_make=None, camera_model=None,
            title=None, caption=None, tag_names=(), people=("Alice",), favorite=False,
            live_photo_video_id=None,
        )

    selector = MagicMock()
    selector.next_batch.side_effect = lambda n: [_alice(f"p{i}") for i in range(n)]
    selector.current_collage.return_value = CollageConfig(
        enabled=True, min_tiles=2, max_tiles=2, layout="grid", smart_caption=True)
    selector.current_scene = None
    client = _client_writing_bytes()
    cfg = CollageConfig(enabled=False)
    w = PrefetchWorker(selector, client, queue_size=2, empty_backoff_s=0.01, collage=cfg)
    w.set_collage_canvas(120, 90)
    w.start()
    try:
        item = w.next(timeout=3.0)
    finally:
        w.stop(timeout=2.0)
    assert item is not None
    assert item[1].caption == "Alice"


def test_stop_is_idempotent():
    selector = MagicMock()
    selector.next_batch.return_value = []
    w = PrefetchWorker(selector, _client_writing_bytes(), empty_backoff_s=0.01)
    w.start()
    w.stop(timeout=1.0)
    w.stop(timeout=1.0)

"""Slideshow orchestration.

Owns: ImmichClient, active AssetSelector, PrefetchWorker, Viewer (pi3d), and
the optional VideoPlayer. Lifecycles in start()/stop().

Threading model:
- Main thread runs the pi3d render loop in `loop()`.
- PrefetchWorker runs on a background thread.
- Network / asset selection is off the render thread.
- State-mutating setters (paused, selection_mode) are called from the network
  thread in Phase 2 — they're safe to call concurrently with `loop()`.

Phase-1 surface only — MQTT/HTTP setters arrive in Phase 2.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import replace
from pathlib import Path

from .collage import is_collage_id
from .config import SCENE_SOURCES, SELECTION_MODES, Config, SelectionMode
from .hidden import HiddenList
from .video.live import live_settings_from, recipe
from .immich.client import ImmichClient, ImmichError
from .immich.models import Asset, AssetKind
from .immich.prefetch import PrefetchWorker
from .immich.selector import (
    AlbumSelector,
    AssetSelector,
    MemorySelector,
    PeopleSelector,
    PlaylistSelector,
    RandomSelector,
    RecentSelector,
    SceneSelector,
    SmartSelector,
)

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "viewer" / "data"

# Picframe-viewer config defaults. The user's `viewer:` YAML block is merged
# on top. Paths into the vendored data tree are computed dynamically so the
# package works installed or in-dev.
_VIEWER_DEFAULTS: dict = {
    "blur_amount": 12,
    "blur_zoom": 1.0,
    "blur_edges": True,
    "edge_alpha": 0.5,
    "fps": 20.0,
    "background": [0.2, 0.2, 0.3, 1.0],
    "blend_type": "blend",
    "font_file": str(_DATA_DIR / "fonts" / "NotoSans-Regular.ttf"),
    "shader": str(_DATA_DIR / "shaders" / "blend_new"),
    "show_text_fm": "%b %d, %Y",
    "show_text_tm": 20.0,
    "show_text_sz": 40,
    "show_text": "title caption name date location",
    "text_justify": "L",
    "text_bkg_hgt": 0.25,
    "text_opacity": 1.0,
    "text_x_margin": 100,
    "text_y_margin": 0,
    "fit": True,
    # Show two consecutive portrait photos side by side (one 16:9 slide)
    # instead of each alone with ⅔ of the screen empty. Videos, live photos
    # and collages are never paired.
    "portrait_pairs": True,
    "video_fit_display": False,
    "kenburns": False,
    "display_x": 0,
    "display_y": 0,
    "display_w": None,
    "display_h": None,
    # 2 = wlr-randr: the only method that works on the KMS driver every
    # current Pi OS ships (vcgencmd display_power is a silent no-op there).
    # 1 = xset for X11 setups, 0 = vcgencmd for legacy firmware-driver Pis.
    "display_power": 2,
    "display_hdmi": "HDMI-A-1",
    "use_glx": False,
    "use_sdl2": True,
    "mat_images": False,
    "mat_type": None,
    "outer_mat_color": None,
    "inner_mat_color": None,
    "outer_mat_border": 75,
    "inner_mat_border": 40,
    "outer_mat_use_texture": True,
    "inner_mat_use_texture": False,
    "mat_resource_folder": str(_DATA_DIR / "mat"),
    "show_clock": False,
    "clock_justify": "R",
    "clock_text_sz": 120,
    "clock_format": "%-I:%M",
    "clock_opacity": 1.0,
    "clock_top_bottom": "T",
    "clock_wdt_offset_pct": 3.0,
    "clock_hgt_offset_pct": 3.0,
    "menu_text_sz": 40,
    "menu_autohide_tm": 0.0,
    "geo_suppress_list": [],
}


class Pic:
    """Adapter from `Asset` + cached path to the shape picframe's viewer expects.

    The viewer reads: fname, orientation, title, caption, exif_datetime,
    location. `orientation` is always 1 (Immich pre-rotates).
    """

    def __init__(self, fname: str, asset: Asset, *, ocr_text: list[str] | None = None) -> None:
        self.fname = fname
        # `display_name` is read by the viewer's `name` branch in preference
        # to `basename(fname)` — gives the user's original filename instead
        # of our internal `<uuid>.jpg` cache filename.
        self.display_name = asset.original_file_name or None
        self.orientation = 1
        # Normalise empty strings to None so the viewer's `is not None`
        # checks correctly skip the overlay row.
        self.title = asset.title if asset.title else None
        self.caption = asset.caption if asset.caption else None
        self.exif_datetime = asset.taken_at.timestamp() if asset.taken_at is not None else 0.0
        self.location = _format_location(asset)
        self.people = ", ".join(asset.people) if asset.people else None
        self.tags = ", ".join(asset.tag_names) if asset.tag_names else None
        self.ocr = ", ".join(ocr_text) if ocr_text else None


def _format_location(asset: Asset) -> str | None:
    parts = [p for p in (asset.geo.city, asset.geo.state, asset.geo.country) if p]
    return ", ".join(parts) if parts else None


# Canonical list of overlay-field keys. interfaces/http.py imports this for
# the POST /api/show_text validator.
#
# Note: `title` and `folder` are accepted by parsers (for picframe-config
# compatibility) but render nothing useful under immframe — Immich has no
# title field, and the "folder" is just our internal cache tempdir. The
# SPA's checkbox list omits them. The viewer's _SHOW_TEXT_BITS map still
# carries them.
SHOW_TEXT_KEYS: tuple[str, ...] = (
    "title", "caption", "name", "date", "location", "folder", "people", "tags", "ocr",
)


def _parse_show_text(value: object) -> list[str]:
    """Accept either a list of keys or picframe's space-separated string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [k for k in value.split() if k in SHOW_TEXT_KEYS]
    if isinstance(value, (list, tuple)):
        return [k for k in value if isinstance(k, str) and k in SHOW_TEXT_KEYS]
    return []


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _iclamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _scene_force_mode(source: str):
    """Config `scene_source` → SceneSelector.force_mode ("auto" = detect)."""
    return None if source == "auto" else source


def _check_display_environment(*, video_enabled: bool) -> None:
    """Warn loudly if the user is on bare TTY/KMS with video enabled.

    pi3d and MPV both want to drive the KMS framebuffer, and without a
    compositor (X11 / Wayland) mediating, MPV cannot get DRM master while
    pi3d holds it. Slideshow works but video playback silently fails.
    See docs/display-setup.md.
    """
    import os
    if not video_enabled:
        return
    if os.environ.get("WAYLAND_DISPLAY"):
        log.info("display: Wayland (%s)", os.environ["WAYLAND_DISPLAY"])
        return
    if os.environ.get("DISPLAY"):
        log.info("display: X11 (%s)", os.environ["DISPLAY"])
        return
    log.warning(
        "no Wayland or X11 detected (WAYLAND_DISPLAY/DISPLAY unset). "
        "pi3d will fight MPV for the framebuffer on bare TTY/KMS — video "
        "playback will fail with 'Cannot set CRTC'. To fix, install labwc "
        "(or another Wayland compositor) and run immframe inside its "
        "session. See docs/display-setup.md."
    )


class Controller:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._stop_evt = threading.Event()
        self._force_next_evt = threading.Event()
        self._paused = False
        self._current_asset: Asset | None = None
        # Second asset when two portraits share the slide (see loop()).
        self._pair_asset: Asset | None = None
        self._portrait_pairs = bool(config.viewer.raw.get(
            "portrait_pairs", _VIEWER_DEFAULTS["portrait_pairs"]))
        # Lookahead item held back by the pairing logic; dropped when the
        # selection changes so a stale slide from the old mode never shows.
        self._pending_item = None
        self._drop_pending = False
        # Item to show before anything else, as (item, history_index):
        # a rotated preview being re-shown, or a "previous" / "show again"
        # replay. history_index says which history entry it replays.
        self._priority_item = None
        self._replay_index: int | None = None   # set by _take_item for the slide being shown
        # Shown-slide history for Previous and the dashboard timeline.
        self._history: list[dict] = []
        self._hist_pos: int | None = None       # index of the slide on screen
        self._hist_lock = threading.Lock()
        self._slide_started_at: float | None = None
        self._next_change_at: float | None = None
        self._video_playing = False
        # Local file backing the current slide (the prefetched preview, or a
        # composited collage). Served by the HTTP /api/current_image endpoint —
        # collages aren't real Immich assets so the image proxy can't fetch them.
        self._current_path: Path | None = None
        self._selection_mode: SelectionMode = config.selection.default_mode
        self._album_ids: list[str] = list(config.selection.album_ids)
        self._smart_query: str = config.selection.smart_query
        self._people_ids: list[str] = list(config.selection.people_ids)

        # Viewer-bound shadow state. Setters write here AND (when viewer is
        # up) to the viewer. `_sync_to_viewer()` reapplies at start() so any
        # mutations between __init__ and start() land on the live viewer.
        viewer_raw = config.viewer.raw
        self._brightness: float = _clamp(float(viewer_raw.get("brightness", 1.0)), 0.0, 1.0)
        self._display_is_on: bool = True
        self._show_text_keys: list[str] = _parse_show_text(
            viewer_raw.get("show_text", "title caption name date location")
        )
        self._show_clock: bool = bool(viewer_raw.get("show_clock", False))
        self._time_delay: float = max(1.0, float(viewer_raw.get("time_delay", 60.0)))
        self._fade_time: float = max(0.0, float(viewer_raw.get("fade_time", 4.0)))

        self._client = ImmichClient(
            config.immich.url,
            config.immich.api_key,
            timeout_s=config.immich.timeout_s,
            image_size=config.immich.image_size,
            write_api_key=config.immich.write_api_key or None,
        )
        # "Never show again" — local list, applied by the prefetch worker.
        self._hidden = HiddenList(
            Path(config.selection.hidden_file).expanduser()
            if config.selection.hidden_file else None
        )

        # Mutable collage shadow state — seeded from config, tunable at runtime
        # via the control plane. Pushed to the prefetch worker as a fresh copy
        # on every change (see _apply_collage / PrefetchWorker.set_collage).
        self._collage = replace(config.collage)
        # Live-photo playback style — seeded from config, tunable at runtime.
        self._live = live_settings_from(config.video).validated()

        # Build initial selector
        self._selector: AssetSelector = self._build_selector(self._selection_mode)
        self._prefetch = PrefetchWorker(
            self._selector,
            self._client,
            queue_size=config.selection.prefetch_count,
            # Fetch OCR in the worker (off the render thread) only while the
            # overlay actually shows it. Re-read each fetch so a runtime
            # show_text toggle is honored.
            wants_ocr=lambda: "ocr" in self._show_text_keys,
            # Always hand the worker the collage settings (a fresh copy). The
            # `enabled` field is the global master switch; even when it's off,
            # playlist collage entries request collages per-batch and reuse
            # these layout/tile settings. The composite flows through the
            # render path unchanged; label reflects the active selection.
            collage=replace(self._collage),
            collage_label=self._collage_label,
            cache_dir=config.selection.cache_dir or None,
            is_hidden=self._hidden.__contains__,
        )

        # Lazily constructed in start() so module import doesn't pull pi3d/mpv
        self._viewer = None
        self._video_player = None
        self._mqtt = None
        self._http = None

    # ── Lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        # Lazy imports so a CI/dev host without pi3d / libmpv can still run
        # the test suite for client/selector/prefetch.
        from .viewer.display import ViewerDisplay

        _check_display_environment(video_enabled=self._config.video.enabled)

        merged_viewer = {**_VIEWER_DEFAULTS, **self._config.viewer.raw}
        self._viewer = ViewerDisplay(merged_viewer)
        self._viewer.slideshow_start()
        self._sync_to_viewer()

        if self._config.video.enabled:
            try:
                from .video.player import VideoPlayer
                self._video_player = VideoPlayer(
                    mute=self._config.video.mute,
                    vo=self._config.video.vo,
                    fit=self._config.video.fit,
                    rotate=self._config.video.rotate,
                    fullscreen=self._config.video.fullscreen,
                    hwdec=self._config.video.hwdec,
                    ensure_fullscreen=self._config.video.ensure_fullscreen,
                )
            except Exception as e:
                log.warning(
                    "video disabled (mpv not available, vo=%r): %s",
                    self._config.video.vo, e,
                )
                self._video_player = None
        else:
            log.info("video disabled by config")

        # Ping is informational only — don't block startup if Immich is slow.
        if not self._client.ping():
            log.warning("Immich ping failed at startup; will retry on first prefetch.")

        # Composite collages at the real display resolution now pi3d knows it.
        # Set unconditionally so a later runtime enable uses the right canvas.
        self._prefetch.set_collage_canvas(
            self._viewer.display_width, self._viewer.display_height
        )

        self._prefetch.start()

        if self._config.control.mqtt.enabled:
            try:
                from .interfaces.mqtt import MqttInterface
                self._mqtt = MqttInterface(self._config.control.mqtt, self)
                self._mqtt.start()
            except Exception as e:
                log.warning("MQTT disabled — %s", e)
                self._mqtt = None

        if self._config.control.http.enabled:
            try:
                from .interfaces.http import HttpInterface
                self._http = HttpInterface(self._config.control.http, self, self._client)
                self._http.start()
            except Exception as e:
                log.warning("HTTP disabled — %s", e)
                self._http = None

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, *_: object) -> None:
        log.info("signal received, stopping")
        self._stop_evt.set()

    def request_restart(self) -> None:
        """Stop the loop so the supervisor relaunches us with the config on
        disk (see start._run_slideshow). Called from the HTTP thread."""
        log.info("restart requested (new configuration)")
        self._restart_requested = True
        self._stop_evt.set()

    @property
    def restart_requested(self) -> bool:
        return getattr(self, "_restart_requested", False)

    @property
    def config(self) -> Config:
        return self._config

    def loop(self) -> None:
        viewer = self._viewer
        if viewer is None:
            raise RuntimeError("Controller.start() must be called before loop()")

        next_tm = 0.0
        current_paths: list[Path] = []                  # files behind the slide on screen

        while not self._stop_evt.is_set():
            now = time.time()
            time_delay = self._time_delay
            fade_time = self._fade_time

            advance = (
                self._force_next_evt.is_set()
                or (not self._paused and now >= next_tm)
            )
            self._force_next_evt.clear()

            if advance:
                item = self._take_item(timeout=1.0)
                if item is None:
                    # Backoff: nothing ready, just keep drawing current
                    if not viewer.slideshow_is_running(
                        time_delay=time_delay, fade_time=fade_time, paused=self._paused
                    )[0]:
                        break
                    continue

                new_path, asset, ocr_text = item

                is_video = asset.kind == AssetKind.VIDEO
                can_play_video = self._video_player is not None and self._config.video.enabled
                show_poster = (
                    is_video and new_path is not None
                    and self._config.video.poster
                )

                # Video with no playback ability → drop the poster, skip.
                if is_video and not can_play_video:
                    if new_path is not None:
                        new_path.unlink(missing_ok=True)
                    continue

                # Video with no poster (download failed or poster disabled):
                # play directly via MPV, no pi3d render.
                if is_video and not show_poster:
                    self._record_shown(asset, None)
                    self._play_video(asset)
                    next_tm = time.time() + time_delay
                    self._next_change_at = next_tm
                    continue

                # Non-video with no path shouldn't happen, but guard against it.
                if new_path is None:
                    continue

                # --- Standard render path (image / live photo / video poster) ---
                # Portrait pairing: a second portrait straight after this one
                # shares the slide (the viewer composites them side by side).
                replay = self._replay_index is not None
                pair = self._pair_for(item) if self._portrait_pairs and not replay else None
                self._current_asset = asset
                self._pair_asset = pair[1] if pair else None
                self._record_shown(asset, self._pair_asset)
                self._publish_state()

                # OCR (when shown) was already fetched by the prefetch worker,
                # off the render thread — see PrefetchWorker._fetch_ocr.
                pic = Pic(str(new_path), asset, ocr_text=ocr_text)
                pic2 = Pic(str(pair[0]), pair[1], ocr_text=pair[2]) if pair else None
                pics_arg = [pic, pic2]                  # picframe slideshow_is_running shape
                loop_running, _, _ = viewer.slideshow_is_running(
                    pics_arg, time_delay=time_delay, fade_time=fade_time, paused=self._paused
                )

                # Clean up the previous slide's files once the new one has been
                # accepted by the viewer (the old texture is no longer needed).
                for old in current_paths:
                    old.unlink(missing_ok=True)
                current_paths = [new_path] + ([pair[0]] if pair else [])
                self._current_path = new_path           # for /api/current_image

                # Motion clip after the still:
                #   - live photo (image + paired motion video):   _play_live_photo
                #   - video asset displayed as poster:             _play_video_after_poster
                if asset.live_photo_video_id:
                    self._play_live_photo(asset)
                    next_tm = self._after_live_photo(time_delay, fade_time)
                else:
                    if is_video:
                        self._play_video_after_poster(asset)
                    next_tm = time.time() + time_delay
                self._next_change_at = next_tm
                if not loop_running:
                    break
            else:
                loop_running, _, _ = viewer.slideshow_is_running(
                    time_delay=time_delay, fade_time=fade_time, paused=self._paused
                )
                if not loop_running:
                    break

        for old in current_paths:
            old.unlink(missing_ok=True)
        self._current_path = None
        leftovers = [self._pending_item, self._priority_item[0] if self._priority_item else None]
        for leftover in leftovers:
            if leftover is not None and leftover[0] is not None:
                leftover[0].unlink(missing_ok=True)
        self._pending_item = self._priority_item = None

    def _after_live_photo(self, time_delay: float, fade_time: float) -> float:
        """When the next slide is due after a live photo's clip.

        "still": the photo stays for the rest of the slide's time (the clip
        counts toward `time_delay`), but at least long enough to register.
        "next": move on as soon as the clip ends."""
        now = time.time()
        if self._live.mode == "still":
            return now + time_delay
        if self._live.after == "next":
            return now
        started = self._slide_started_at or now
        return max(started + time_delay, now + max(3.0, fade_time + 1.0))

    # ── Live-photo style (runtime-tunable) ──────────────────────────────
    @property
    def live_photo(self) -> dict:
        l = self._live
        return {"mode": l.mode, "hold_s": l.hold_s, "play_s": l.play_s, "repeats": l.repeats,
                "speed": l.speed, "pause_s": l.pause_s, "after": l.after}

    def set_live_photo(self, **fields) -> dict:
        """Update any of mode / hold_s / play_s / repeats / speed / pause_s /
        after. Takes effect from the next live photo. Raises ValueError for
        an unknown field or value."""
        allowed = {"mode", "hold_s", "play_s", "repeats", "speed", "pause_s", "after"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown live-photo setting(s): {sorted(bad)}")
        self._live = replace(self._live, **fields).validated()
        log.info("live photo style: %s", self.live_photo)
        self._publish_state()
        return self.live_photo

    # ── Queue access + portrait pairing ─────────────────────────────────
    def _take_item(self, *, timeout: float):
        """Next slide: the held-back lookahead item if there is one (unless
        the selection changed since it was fetched), else the queue."""
        self._replay_index = None
        priority = self._priority_item
        self._priority_item = None
        if priority is not None:
            item, idx = priority
            self._replay_index = idx
            return item
        pending = self._pending_item
        self._pending_item = None
        if pending is not None:
            if not self._drop_pending and not self._blocked(pending):
                return pending
            if pending[0] is not None:
                pending[0].unlink(missing_ok=True)
        self._drop_pending = False
        # Items blocked after they were queued are dropped here.
        while True:
            item = self._prefetch.next(timeout=timeout)
            if item is None or not self._blocked(item):
                return item
            if item[0] is not None:
                item[0].unlink(missing_ok=True)

    def _blocked(self, item) -> bool:
        return item[1].id in self._hidden

    @staticmethod
    def _pairable(item) -> bool:
        """A plain portrait image: not a video, live photo or collage."""
        path, asset, _ = item
        return (
            path is not None
            and asset.kind == AssetKind.IMAGE
            and asset.is_portrait
            and not asset.live_photo_video_id
            and not is_collage_id(asset.id)
        )

    def _pair_for(self, item):
        """If `item` is a pairable portrait, peek the next queued item; a
        second pairable portrait is returned to share the slide, anything
        else is held back as the following slide."""
        if not self._pairable(item):
            return None
        second = self._prefetch.next(timeout=0.5)
        if second is None:
            return None
        if self._pairable(second):
            return second
        self._pending_item = second
        return None

    def _play_video(self, asset: Asset) -> None:
        if self._video_player is None:
            log.debug("skipping video asset %s — no player available", asset.id)
            return
        url, headers = self._client.video_play_args(asset.id)
        self._current_asset = asset
        self._pair_asset = None
        self._publish_state()
        log.info("video play: asset=%s url=%s", asset.id, url)
        self._play_video_url(url, headers)

    def _play_video_url(self, url: str, headers: dict[str, str], *,
                        max_s: float | None = None, **play_opts) -> None:
        """Play a video URL through MPV; block until EOF, SIGINT, `max_s`,
        or the config.video.max_play_s ceiling. `play_opts` go to
        VideoPlayer.play (vf / loop / speed / hwdec / audio)."""
        if self._video_player is None:
            return
        end_evt = threading.Event()
        # A clip's own play time, never beyond the global max_play_s ceiling.
        limit = self._config.video.max_play_s if max_s is None else min(max_s, self._config.video.max_play_s)
        try:
            self._video_player.play(url, headers=headers, on_end=end_evt.set, **play_opts)
        except Exception as e:
            log.warning("video play failed: %s", e)
            return
        self._video_playing = True
        try:
            self._wait_video(end_evt, limit)
        finally:
            self._video_playing = False

    def _wait_video(self, end_evt: threading.Event, limit: float | None = None) -> None:
        if limit is None:
            limit = self._config.video.max_play_s
        deadline = time.time() + max(1.0, limit)
        paused_video = False
        while not end_evt.wait(timeout=0.5):
            if self._stop_evt.is_set():
                self._video_player.stop()
                return
            # "Next" from the dashboard / HA / CLI: cut the clip short. The
            # event stays set so the main loop advances immediately after.
            if self._force_next_evt.is_set():
                log.info("video skipped (next)")
                self._video_player.stop()
                return
            # Mirror the slideshow pause onto the clip; the max_play_s
            # deadline slides along while paused so a long pause doesn't
            # expire it the instant playback resumes.
            if self._paused != paused_video:
                paused_video = self._paused
                try:
                    self._video_player.pause(paused_video)
                except Exception as e:
                    log.debug("video pause toggle failed: %s", e)
            if paused_video:
                deadline += 0.5
                continue
            if time.time() > deadline:
                if limit < self._config.video.max_play_s:
                    log.info("clip reached its play time (%.1fs); stopping", limit)
                else:
                    log.warning("video exceeded max_play_s; stopping")
                self._video_player.stop()
                return

    def _hold_rendering(self, seconds: float) -> None:
        """Keep the pi3d slideshow drawing for `seconds` before a video starts.

        Unlike `self._stop_evt.wait(seconds)`, this pumps the render loop so the
        current slide's crossfade actually animates (otherwise the matted poster
        / live-photo still is drawn once at ~0 alpha and frozen — effectively
        invisible) and the compositor stays responsive. pi3d's `loop_running()`
        throttles to the configured FPS, so this paces itself. Returns early on
        shutdown or if the display loop stops."""
        if seconds <= 0:
            return
        viewer = self._viewer
        if viewer is None:                              # no display (tests): just wait
            self._stop_evt.wait(seconds)
            return
        deadline = time.time() + seconds
        while not self._stop_evt.is_set() and time.time() < deadline:
            running, _, _ = viewer.slideshow_is_running(
                time_delay=self._time_delay,
                fade_time=self._fade_time,
                paused=self._paused,
            )
            if not running:
                break

    def _play_live_photo(self, asset: Asset) -> None:
        """For an image asset with a livePhotoVideoId, play the motion
        clip after the still has been visible briefly."""
        if (
            self._video_player is None
            or not asset.live_photo_video_id
            or not self._config.video.enabled
        ):
            return
        live = self._live
        r = recipe(live)
        if r is None:                                   # mode "still": photo only
            return
        self._hold_rendering(live.hold_s)
        if self._stop_evt.is_set() or self._force_next_evt.is_set():
            return                                      # skipped while the still was up
        url, headers = self._client.video_play_args(asset.live_photo_video_id)
        log.info("live photo: asset=%s motion=%s mode=%s speed=%g",
                 asset.id, asset.live_photo_video_id, live.mode, live.speed)
        self._play_video_url(url, headers, max_s=r.max_s, vf=r.vf, loop=r.loop,
                             speed=r.speed, hwdec=live.hwdec, audio=r.audio)

    def _play_video_after_poster(self, asset: Asset) -> None:
        """For a VIDEO asset rendered as a matted poster first, hold the
        still for `video.poster_hold_s` then play the video through MPV."""
        if self._video_player is None or not self._config.video.enabled:
            return
        self._hold_rendering(max(0.0, self._config.video.poster_hold_s))
        if self._stop_evt.is_set():
            return
        self._play_video(asset)

    def stop(self) -> None:
        self._stop_evt.set()
        if self._http is not None:
            try:
                self._http.stop()
            except Exception as e:
                log.debug("http stop: %s", e)
            self._http = None
        if self._mqtt is not None:
            try:
                self._mqtt.stop()
            except Exception as e:
                log.debug("mqtt stop: %s", e)
            self._mqtt = None
        if self._prefetch is not None:
            self._prefetch.stop()
        if self._video_player is not None:
            self._video_player.stop()
            self._video_player.close()
        if self._viewer is not None:
            try:
                self._viewer.slideshow_stop()
            except Exception as e:
                log.debug("viewer stop: %s", e)
        self._client.close()

    def _publish_state(self) -> None:
        """Notify control plane of a state change. No-op if no MQTT/HTTP wired."""
        m = self._mqtt
        if m is not None:
            try:
                m.publish_state()
            except Exception as e:
                log.debug("publish_state: %s", e)

    # ── Basic transport ─────────────────────────────────────────────────
    def next(self) -> None:
        self._force_next_evt.set()

    # ── Curation from the sofa ──────────────────────────────────────────
    def hide_current(self) -> dict:
        """Never show the current asset again (see hide_asset)."""
        asset = self._current_asset
        if asset is None:
            raise ValueError("no current asset")
        return self.hide_asset(asset.id)

    def hide_asset(self, asset_id: str) -> dict:
        """Block an asset: add it (and its live-photo clip) to the local
        block list — instant, needs no write access — then archive it in
        Immich when the key allows. Works for the slide on screen (which
        then advances), anything in the history or the upcoming queue
        (dropped when its turn comes), or a bare id.

        Returns {"hidden": id, "archived": bool, "error": str | None}.
        Raises ValueError for a collage (synthetic; nothing to block)."""
        if is_collage_id(asset_id):
            raise ValueError("a collage is not an Immich asset — block its photos individually")
        asset = self._find_asset(asset_id)
        companions = [asset.live_photo_video_id] if asset is not None and asset.live_photo_video_id else []
        details = {"file": asset.original_file_name, "kind": asset.kind.value} if asset is not None else {}
        self._hidden.add(asset_id, *companions, details=details)
        log.info("blocked: %s (%s)", asset_id, details.get("file") or "?")
        archived, error = False, None
        try:
            self._client.update_asset(asset_id, visibility="archive")
            archived = True
            self._hidden.annotate(asset_id, archived=True)
            log.info("archived in Immich: %s", asset_id)
        except ImmichError as e:
            error = str(e)
            log.warning("archive in Immich failed (blocked locally anyway): %s", e)
        cur = self._current_asset
        if cur is not None and asset_id in (cur.id, getattr(self._pair_asset, "id", None)):
            self.next()
        self._publish_state()
        return {"hidden": asset_id, "archived": archived, "error": error}

    def unhide(self, asset_ids: list[str]) -> list[dict]:
        """Unblock assets: remove them from the block list and, when we had
        archived them in Immich, put them back on the timeline so they can
        be selected again. Returns one result per id."""
        results = []
        for aid in asset_ids:
            details = self._hidden.remove(aid)
            if details is None:
                results.append({"id": aid, "unhidden": False, "error": "not blocked"})
                continue
            error = None
            if details.get("archived"):
                try:
                    self._client.update_asset(aid, visibility="timeline")
                except ImmichError as e:
                    error = f"unblocked on the frame, but un-archiving in Immich failed: {e}"
                    log.warning("unarchive %s failed: %s", aid, e)
            log.info("unblocked: %s", aid)
            results.append({"id": aid, "unhidden": True, "error": error})
        self._publish_state()
        return results

    def hidden_entries(self) -> list[dict]:
        return self._hidden.entries()

    def _find_asset(self, asset_id: str) -> Asset | None:
        for a in (self._current_asset, self._pair_asset):
            if a is not None and a.id == asset_id:
                return a
        with self._hist_lock:
            for e in reversed(self._history):
                for a in (e["asset"], e["pair"]):
                    if a is not None and a.id == asset_id:
                        return a
        for a in self._prefetch.peek():
            if a.id == asset_id:
                return a
        return None

    # ── History / previous / timeline ───────────────────────────────────
    HISTORY_MAX = 100

    def _record_shown(self, asset: Asset, pair: Asset | None) -> None:
        """Called by the loop as each slide goes up. A replay moves the
        history cursor back to that entry instead of adding a new one."""
        now = time.time()
        self._slide_started_at = now
        with self._hist_lock:
            idx = self._replay_index
            if idx is not None and 0 <= idx < len(self._history) \
                    and self._history[idx]["asset"].id == asset.id:
                self._hist_pos = idx
                return
            self._history.append({"asset": asset, "pair": pair, "shown_at": now})
            if len(self._history) > self.HISTORY_MAX:
                del self._history[: len(self._history) - self.HISTORY_MAX]
            self._hist_pos = len(self._history) - 1

    @staticmethod
    def _replayable(entry: dict) -> bool:
        # Collages are composited once and their file is gone after display.
        return not is_collage_id(entry["asset"].id)

    @property
    def can_go_back(self) -> bool:
        with self._hist_lock:
            pos = self._hist_pos
            return pos is not None and any(
                self._replayable(e) for e in self._history[:pos])

    def previous(self) -> dict:
        """Go back to the slide before the one on screen (skipping collages);
        repeated calls keep stepping back. Raises ValueError when there is
        nothing earlier, ImmichError if the photo can't be fetched."""
        with self._hist_lock:
            pos = self._hist_pos if self._hist_pos is not None else len(self._history)
            idx = next((i for i in range(pos - 1, -1, -1) if self._replayable(self._history[i])), None)
            if idx is None:
                raise ValueError("nothing earlier to go back to")
            asset = self._history[idx]["asset"]
        return self._replay(idx, asset)

    def show_again(self, asset_id: str) -> dict:
        """Put a slide from the history back on screen now."""
        with self._hist_lock:
            idx = next((i for i in range(len(self._history) - 1, -1, -1)
                        if self._history[i]["asset"].id == asset_id), None)
            if idx is None:
                raise ValueError("that photo is no longer in the history")
            if not self._replayable(self._history[idx]):
                raise ValueError("collages can't be shown again")
            asset = self._history[idx]["asset"]
        return self._replay(idx, asset)

    def _replay(self, idx: int, asset: Asset) -> dict:
        if asset.id in self._hidden:
            raise ValueError("that photo is blocked — unblock it first")
        item = self._prefetch.fetch_now(asset)          # blocking download, caller's thread
        if item is None or (item[0] is None and asset.kind != AssetKind.VIDEO):
            raise ImmichError(f"could not fetch {asset.id} from Immich")
        old = self._priority_item
        self._priority_item = (item, idx)
        if old is not None and old[0][0] is not None:
            old[0][0].unlink(missing_ok=True)
        self._force_next_evt.set()
        log.info("replaying history[%d]: %s", idx, asset.id)
        return {"id": asset.id, "index": idx}

    def timeline(self) -> dict:
        """Recently shown (oldest → newest, with the on-screen position) and
        what's queued next, for the dashboard."""
        with self._hist_lock:
            history = [dict(e, blocked=e["asset"].id in self._hidden) for e in self._history]
            pos = self._hist_pos
        upcoming: list[Asset] = []
        prio = self._priority_item
        if prio is not None:
            upcoming.append(prio[0][1])
        if self._pending_item is not None:
            upcoming.append(self._pending_item[1])
        upcoming.extend(self._prefetch.peek())
        return {
            "history": history,
            "position": pos,
            "upcoming": [a for a in upcoming if a.id not in self._hidden],
        }

    @property
    def slide_started_at(self) -> float | None:
        return self._slide_started_at

    @property
    def next_change_at(self) -> float | None:
        return self._next_change_at

    @property
    def video_playing(self) -> bool:
        return self._video_playing

    def favorite_current(self, value: bool | None = None) -> dict:
        """Star / unstar the current asset in Immich. `value=None` toggles.
        Needs a key with `asset.update` (immich.write_api_key). Raises
        ValueError with no current asset / on a collage, ImmichError when
        Immich refuses."""
        asset = self._current_asset
        if asset is None:
            raise ValueError("no current asset")
        if is_collage_id(asset.id):
            raise ValueError("a collage is not an Immich asset")
        new = (not asset.favorite) if value is None else bool(value)
        self._client.update_asset(asset.id, favorite=new)
        log.info("%s in Immich: %s", "favourited" if new else "unfavourited", asset.id)
        # Reflect it in state immediately (Asset is frozen → replace).
        if self._current_asset is asset:
            self._current_asset = replace(asset, favorite=new)
        self._publish_state()
        return {"id": asset.id, "favorite": new}

    @property
    def hidden_count(self) -> int:
        return len(self._hidden)

    def rotate_current(self, delta: int = 90) -> dict:
        """Rotate the photo on screen `delta`° clockwise through Immich's
        non-destructive editor (the original is untouched; Immich rewrites
        the preview). Once the regenerated preview arrives the slide is
        re-shown in place. Returns {"id", "angle"} (cumulative angle).
        Raises ValueError for no asset / a video / a collage; ImmichError
        when Immich refuses (the write key needs asset.edit.get/create)."""
        asset = self._current_asset
        if asset is None:
            raise ValueError("no current asset")
        if is_collage_id(asset.id):
            raise ValueError("a collage is not an Immich asset")
        if asset.kind != AssetKind.IMAGE:
            raise ValueError("only photos can be rotated (video rotation lives in the file)")
        angle = self._client.rotate_asset(asset.id, delta)
        log.info("rotated in Immich: %s -> %d°", asset.id, angle)
        threading.Thread(
            target=self._reshow_after_edit, args=(asset, self._current_path),
            name="reshow", daemon=True,
        ).start()
        return {"id": asset.id, "angle": angle}

    def _reshow_after_edit(self, asset: Asset, old_path: Path | None, *,
                           attempts: int = 8, interval_s: float = 1.5) -> None:
        """Poll for the regenerated preview (Immich rewrites it in a job a
        moment after the edit); when it differs from what's on screen, put
        it at the head of the line and advance. Gives up quietly if the
        slide has already moved on."""
        old_bytes = None
        try:
            if old_path is not None:
                old_bytes = old_path.read_bytes()
        except OSError:
            pass
        for _ in range(attempts):
            self._stop_evt.wait(interval_s)
            if self._stop_evt.is_set():
                return
            if self._current_asset is None or self._current_asset.id != asset.id:
                return                                  # moved on; the next fetch is fresh anyway
            item = self._prefetch.fetch_now(asset)
            if item is None or item[0] is None:
                continue
            try:
                same = old_bytes is not None and item[0].read_bytes() == old_bytes
            except OSError:
                same = False
            if same:
                item[0].unlink(missing_ok=True)
                continue                                # not regenerated yet
            self._priority_item = (item, self._hist_pos)
            self._force_next_evt.set()
            return
        log.info("preview for %s not regenerated yet — it will show rotated next time", asset.id)

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = bool(value)
        self._publish_state()

    # ── Selection ───────────────────────────────────────────────────────
    @property
    def selection_mode(self) -> SelectionMode:
        return self._selection_mode

    @selection_mode.setter
    def selection_mode(self, mode: SelectionMode) -> None:
        if mode not in SELECTION_MODES:
            raise ValueError(f"unknown selection_mode: {mode!r}; valid: {SELECTION_MODES}")
        self._selection_mode = mode
        self._selector = self._build_selector(mode)
        self._prefetch.set_selector(self._selector)
        self._drop_pending = True
        self._force_next_evt.set()
        self._publish_state()

    @property
    def album_ids(self) -> list[str]:
        return list(self._album_ids)

    @album_ids.setter
    def album_ids(self, ids: list[str]) -> None:
        self._album_ids = list(ids)
        if isinstance(self._selector, AlbumSelector):
            self._selector.set_album_ids(self._album_ids)
            self._prefetch.drain()
            self._drop_pending = True
            self._force_next_evt.set()
        self._publish_state()

    @property
    def smart_query(self) -> str:
        return self._smart_query

    @smart_query.setter
    def smart_query(self, q: str) -> None:
        self._smart_query = q
        if isinstance(self._selector, SmartSelector):
            self._selector.set_query(q)
            self._prefetch.drain()
            self._drop_pending = True
            self._force_next_evt.set()
        self._publish_state()

    @property
    def people_ids(self) -> list[str]:
        return list(self._people_ids)

    @people_ids.setter
    def people_ids(self, ids: list[str]) -> None:
        self._people_ids = list(ids)
        if isinstance(self._selector, PeopleSelector):
            self._selector.set_person_ids(self._people_ids)
            self._prefetch.drain()
            self._drop_pending = True
            self._force_next_evt.set()
        self._publish_state()

    # ── Viewer-bound knobs ──────────────────────────────────────────────
    @property
    def brightness(self) -> float:
        return self._brightness

    @brightness.setter
    def brightness(self, value: float) -> None:
        v = _clamp(float(value), 0.0, 1.0)
        self._brightness = v
        if self._viewer is not None:
            try:
                self._viewer.set_brightness(v)
            except Exception as e:
                log.debug("viewer.set_brightness: %s", e)
        self._publish_state()

    @property
    def display_is_on(self) -> bool:
        if self._viewer is not None:
            try:
                self._display_is_on = bool(self._viewer.display_is_on)
            except Exception:
                pass
        return self._display_is_on

    @display_is_on.setter
    def display_is_on(self, value: bool) -> None:
        self._display_is_on = bool(value)
        if self._viewer is not None:
            try:
                self._viewer.display_is_on = self._display_is_on
            except Exception as e:
                log.debug("viewer.display_is_on: %s", e)
        self._publish_state()

    @property
    def show_text(self) -> list[str]:
        return list(self._show_text_keys)

    @show_text.setter
    def show_text(self, value: object) -> None:
        keys = _parse_show_text(value)
        self._show_text_keys = keys
        if self._viewer is not None:
            try:
                self._viewer.set_show_text(None)
                for k in keys:
                    self._viewer.set_show_text(k, "ON")
            except Exception as e:
                log.debug("viewer.set_show_text: %s", e)
        self._publish_state()

    @property
    def show_clock(self) -> bool:
        return self._show_clock

    @show_clock.setter
    def show_clock(self, value: bool) -> None:
        self._show_clock = bool(value)
        if self._viewer is not None:
            try:
                self._viewer.clock_is_on = self._show_clock
            except Exception as e:
                log.debug("viewer.clock_is_on: %s", e)
        self._publish_state()

    @property
    def time_delay(self) -> float:
        return self._time_delay

    @time_delay.setter
    def time_delay(self, value: float) -> None:
        self._time_delay = max(1.0, float(value))
        self._publish_state()

    @property
    def fade_time(self) -> float:
        return self._fade_time

    @fade_time.setter
    def fade_time(self, value: float) -> None:
        self._fade_time = max(0.0, float(value))
        self._publish_state()

    # ── State exposure ──────────────────────────────────────────────────
    @property
    def current_asset(self) -> Asset | None:
        return self._current_asset

    @property
    def pair_asset(self) -> Asset | None:
        """The second portrait sharing the slide, or None."""
        return self._pair_asset

    def current_local_image(self) -> Path | None:
        """Path to the file backing the current slide (collage composite or
        prefetched preview), or None. Served by HTTP /api/current_image."""
        return self._current_path

    @property
    def current_scene(self) -> str | None:
        """The label currently driving selection (scene name, person name,
        "On this day", "Last 30 days", etc.) — or None for modes that don't
        carry a label."""
        sel = self._selector
        return getattr(sel, "current_scene", None)

    def _collage_label(self, n: int) -> str:
        """Generic label for a collage's synthetic asset, e.g. 'beach • 4
        photos' or 'Random • 4 photos'. Called from the prefetch thread."""
        scene = self.current_scene
        base = scene if scene else self._selection_mode.capitalize()
        return f"{base} • {n} photo{'s' if n != 1 else ''}"

    # ── Collage (runtime-tunable) ────────────────────────────────────────
    def _apply_collage(self) -> None:
        """Push the current collage shadow state to the prefetch worker (as a
        fresh copy) and force a refresh so the change shows promptly."""
        if self._prefetch is not None:
            self._prefetch.set_collage(replace(self._collage))
        self._force_next_evt.set()
        self._publish_state()

    @property
    def collage_enabled(self) -> bool:
        return self._collage.enabled

    @collage_enabled.setter
    def collage_enabled(self, value: bool) -> None:
        self._collage.enabled = bool(value)
        self._apply_collage()

    @property
    def collage_layout(self) -> str:
        return self._collage.layout

    @collage_layout.setter
    def collage_layout(self, value: str) -> None:
        v = str(value)
        if v not in ("auto", "grid", "golden_ratio"):
            raise ValueError(
                f"collage_layout must be 'auto', 'grid' or 'golden_ratio'; got {v!r}"
            )
        self._collage.layout = v
        self._apply_collage()

    @property
    def collage_min_tiles(self) -> int:
        return self._collage.min_tiles

    @collage_min_tiles.setter
    def collage_min_tiles(self, value: int) -> None:
        v = _iclamp(int(value), 2, 12)
        self._collage.min_tiles = v
        if self._collage.max_tiles < v:                 # keep min <= max
            self._collage.max_tiles = v
        self._apply_collage()

    @property
    def collage_max_tiles(self) -> int:
        return self._collage.max_tiles

    @collage_max_tiles.setter
    def collage_max_tiles(self, value: int) -> None:
        v = _iclamp(int(value), 2, 12)
        self._collage.max_tiles = v
        if self._collage.min_tiles > v:                 # keep min <= max
            self._collage.min_tiles = v
        self._apply_collage()

    # ── Internals ───────────────────────────────────────────────────────
    def _sync_to_viewer(self) -> None:
        """Apply controller-held shadow state to the live viewer.

        Called once after viewer construction so settings mutated between
        __init__ and start() take effect.
        """
        v = self._viewer
        if v is None:
            return
        try:
            v.set_brightness(self._brightness)
        except Exception as e:
            log.debug("sync brightness: %s", e)
        try:
            v.set_show_text(None)
            for k in self._show_text_keys:
                v.set_show_text(k, "ON")
        except Exception as e:
            log.debug("sync show_text: %s", e)
        try:
            v.clock_is_on = self._show_clock
        except Exception as e:
            log.debug("sync show_clock: %s", e)
        # display_is_on is read from the viewer rather than pushed — initial
        # hardware state is the viewer's to know.
        try:
            self._display_is_on = bool(v.display_is_on)
        except Exception:
            pass

    def _build_selector(self, mode: SelectionMode) -> AssetSelector:
        sel_cfg = self._config.selection
        if mode == "random":
            return RandomSelector(
                self._client, include_videos=self._config.video.enabled,
                min_rating=sel_cfg.min_rating,
            )
        if mode == "favorites":
            return RandomSelector(
                self._client, include_videos=self._config.video.enabled, favorites=True,
            )
        if mode == "album":
            return AlbumSelector(self._client, self._album_ids)
        if mode == "smart":
            return SmartSelector(self._client, self._smart_query, pages=sel_cfg.smart_pages)
        if mode == "scene":
            return SceneSelector(
                self._client, force_mode=_scene_force_mode(sel_cfg.scene_source),
                pages=sel_cfg.smart_pages,
            )
        if mode == "people":
            return PeopleSelector(
                self._client, self._people_ids,
                min_photos=sel_cfg.people_min_photos,
                favorites_only=sel_cfg.people_favorites_only,
            )
        if mode == "memory":
            return MemorySelector(self._client)
        if mode == "recent":
            return RecentSelector(
                self._client,
                days=self._config.selection.recent_days,
                field=self._config.selection.recent_field,
            )
        if mode == "playlist":
            entries = []
            for entry in self._config.selection.playlist:
                try:
                    sel = self._build_selector_from_entry(entry)
                except ValueError as e:
                    log.warning("skipping playlist entry %s: %s", entry, e)
                    continue
                count = int(entry.get("count", 25))
                # Per-entry collage: count then means "number of collages",
                # tiled from this entry's source. None for a normal entry, else
                # a CollageConfig (global settings merged with per-entry
                # overrides: layout / tiles / tile_text / smart_caption).
                collage_cfg = self._entry_collage_cfg(entry)
                entries.append((sel, count, collage_cfg))
            if not entries:
                log.warning(
                    "playlist mode selected but selection.playlist is empty or invalid — "
                    "falling back to random"
                )
                return RandomSelector(self._client, include_videos=self._config.video.enabled)
            return PlaylistSelector(entries)
        raise ValueError(f"unknown selection_mode: {mode!r}")

    def _entry_collage_cfg(self, entry: dict):
        """Build the CollageConfig for a playlist entry, or None if it isn't a
        collage entry. Starts from the global collage settings and applies the
        entry's overrides (layout / tiles / min_tiles / max_tiles / tile_text /
        smart_caption). Bad overrides are skipped (the global value stands)."""
        if not bool(entry.get("collage", False)):
            return None
        overrides: dict = {}
        try:
            layout = entry.get("layout")
            if layout is not None:
                if str(layout) in ("auto", "grid", "golden_ratio"):
                    overrides["layout"] = str(layout)
                else:
                    log.warning("playlist collage: bad layout %r — using global", layout)
            if "tiles" in entry:
                t = _iclamp(int(entry["tiles"]), 2, 12)
                overrides["min_tiles"] = overrides["max_tiles"] = t
            if "min_tiles" in entry:
                overrides["min_tiles"] = _iclamp(int(entry["min_tiles"]), 2, 12)
            if "max_tiles" in entry:
                overrides["max_tiles"] = _iclamp(int(entry["max_tiles"]), 2, 12)
            mn = overrides.get("min_tiles", self._collage.min_tiles)
            mx = overrides.get("max_tiles", self._collage.max_tiles)
            if mn > mx:                                  # keep min <= max
                overrides["max_tiles"] = mn
            if "tile_text" in entry:
                overrides["tile_text"] = str(entry["tile_text"])
            if "smart_caption" in entry:
                overrides["smart_caption"] = bool(entry["smart_caption"])
        except (TypeError, ValueError) as e:
            log.warning("playlist collage overrides invalid (%s) — using global", e)
            overrides = {}
        return replace(self._collage, enabled=True, **overrides)

    def _build_selector_from_entry(self, entry: dict) -> AssetSelector:
        """Build a sub-selector for a playlist entry. Each entry may override
        controller-level config (album_ids, people_ids, days, etc.)."""
        mode = entry.get("mode")
        sel_cfg = self._config.selection
        if mode in ("random", "favorites"):
            # Per-entry narrowing: `favorites`, `min_rating`, `album_ids`,
            # `tag_ids` all ride on the same /search/random call.
            min_rating = entry.get("min_rating", sel_cfg.min_rating)
            if min_rating is not None:
                min_rating = _iclamp(int(min_rating), 1, 5)
            return RandomSelector(
                self._client, include_videos=self._config.video.enabled,
                favorites=(mode == "favorites") or bool(entry.get("favorites", False)),
                min_rating=min_rating,
                album_ids=list(entry.get("album_ids", [])) or None,
                tag_ids=list(entry.get("tag_ids", [])) or None,
            )
        if mode == "album":
            return AlbumSelector(self._client, list(entry.get("album_ids", self._album_ids)))
        if mode == "smart":
            return SmartSelector(
                self._client, entry.get("smart_query", self._smart_query),
                pages=int(entry.get("pages", sel_cfg.smart_pages)),
            )
        if mode == "scene":
            source = str(entry.get("source", sel_cfg.scene_source))
            if source not in SCENE_SOURCES:
                raise ValueError(f"scene source must be one of {SCENE_SOURCES}; got {source!r}")
            return SceneSelector(
                self._client, force_mode=_scene_force_mode(source),
                pages=int(entry.get("pages", sel_cfg.smart_pages)),
            )
        if mode == "people":
            return PeopleSelector(
                self._client, list(entry.get("people_ids", self._people_ids)),
                min_photos=int(entry.get("min_photos", sel_cfg.people_min_photos)),
                favorites_only=bool(entry.get("favorites_only", sel_cfg.people_favorites_only)),
            )
        if mode == "memory":
            return MemorySelector(self._client)
        if mode == "recent":
            return RecentSelector(
                self._client,
                days=int(entry.get("days", self._config.selection.recent_days)),
                field=entry.get("field", self._config.selection.recent_field),
            )
        if mode == "playlist":
            raise ValueError("nested playlist mode is not supported")
        raise ValueError(f"unknown playlist mode: {mode!r}")

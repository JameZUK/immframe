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

from .config import Config, SelectionMode
from .immich.client import ImmichClient
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
    "video_fit_display": False,
    "kenburns": False,
    "display_x": 0,
    "display_y": 0,
    "display_w": None,
    "display_h": None,
    "display_power": 0,
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
        )

        # Mutable collage shadow state — seeded from config, tunable at runtime
        # via the control plane. Pushed to the prefetch worker as a fresh copy
        # on every change (see _apply_collage / PrefetchWorker.set_collage).
        self._collage = replace(config.collage)

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

    def loop(self) -> None:
        viewer = self._viewer
        if viewer is None:
            raise RuntimeError("Controller.start() must be called before loop()")

        next_tm = 0.0
        current_path: Path | None = None

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
                item = self._prefetch.next(timeout=1.0)
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
                    self._play_video(asset)
                    next_tm = time.time() + time_delay
                    continue

                # Non-video with no path shouldn't happen, but guard against it.
                if new_path is None:
                    continue

                # --- Standard render path (image / live photo / video poster) ---
                self._current_asset = asset
                self._publish_state()

                # OCR (when shown) was already fetched by the prefetch worker,
                # off the render thread — see PrefetchWorker._fetch_ocr.
                pic = Pic(str(new_path), asset, ocr_text=ocr_text)
                pics_arg = [pic, None]                  # picframe slideshow_is_running shape
                loop_running, _, _ = viewer.slideshow_is_running(
                    pics_arg, time_delay=time_delay, fade_time=fade_time, paused=self._paused
                )

                # Clean up the previous slide's file once the new one has been
                # accepted by the viewer (the old texture is no longer needed).
                if current_path is not None:
                    current_path.unlink(missing_ok=True)
                current_path = new_path
                self._current_path = new_path           # for /api/current_image

                # Motion clip after the still:
                #   - live photo (image + paired motion video):   _play_live_photo
                #   - video asset displayed as poster:             _play_video_after_poster
                if asset.live_photo_video_id:
                    self._play_live_photo(asset)
                elif is_video:
                    self._play_video_after_poster(asset)

                next_tm = time.time() + time_delay
                if not loop_running:
                    break
            else:
                loop_running, _, _ = viewer.slideshow_is_running(
                    time_delay=time_delay, fade_time=fade_time, paused=self._paused
                )
                if not loop_running:
                    break

        if current_path is not None:
            current_path.unlink(missing_ok=True)
        self._current_path = None

    def _play_video(self, asset: Asset) -> None:
        if self._video_player is None:
            log.debug("skipping video asset %s — no player available", asset.id)
            return
        url, headers = self._client.video_play_args(asset.id)
        self._current_asset = asset
        self._publish_state()
        log.info("video play: asset=%s url=%s", asset.id, url)
        self._play_video_url(url, headers)

    def _play_video_url(self, url: str, headers: dict[str, str]) -> None:
        """Play a video URL through MPV; block until EOF, SIGINT, or
        config.video.max_play_s ceiling."""
        if self._video_player is None:
            return
        end_evt = threading.Event()
        try:
            self._video_player.play(url, headers=headers, on_end=end_evt.set)
        except Exception as e:
            log.warning("video play failed: %s", e)
            return
        deadline = time.time() + max(1.0, self._config.video.max_play_s)
        while not end_evt.wait(timeout=0.5):
            if self._stop_evt.is_set():
                self._video_player.stop()
                return
            if time.time() > deadline:
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
        self._hold_rendering(max(0.0, self._config.video.live_photo_hold_s))
        if self._stop_evt.is_set():
            return
        url, headers = self._client.video_play_args(asset.live_photo_video_id)
        log.info("live photo: asset=%s motion=%s", asset.id, asset.live_photo_video_id)
        self._play_video_url(url, headers)

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
        valid = ("random", "album", "smart", "scene", "people", "memory", "recent", "playlist")
        if mode not in valid:
            raise ValueError(f"unknown selection_mode: {mode!r}; valid: {valid}")
        self._selection_mode = mode
        self._selector = self._build_selector(mode)
        self._prefetch.set_selector(self._selector)
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
        if mode == "random":
            return RandomSelector(self._client, include_videos=self._config.video.enabled)
        if mode == "album":
            return AlbumSelector(self._client, self._album_ids)
        if mode == "smart":
            return SmartSelector(self._client, self._smart_query)
        if mode == "scene":
            return SceneSelector(self._client)
        if mode == "people":
            return PeopleSelector(self._client, self._people_ids)
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
        if mode == "random":
            return RandomSelector(self._client, include_videos=self._config.video.enabled)
        if mode == "album":
            return AlbumSelector(self._client, list(entry.get("album_ids", self._album_ids)))
        if mode == "smart":
            return SmartSelector(self._client, entry.get("smart_query", self._smart_query))
        if mode == "scene":
            return SceneSelector(self._client)
        if mode == "people":
            return PeopleSelector(self._client, list(entry.get("people_ids", self._people_ids)))
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

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
from pathlib import Path

from .config import Config, SelectionMode
from .immich.client import ImmichClient
from .immich.models import Asset, AssetKind
from .immich.prefetch import PrefetchWorker
from .immich.selector import (
    AlbumSelector,
    AssetSelector,
    RandomSelector,
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

    def __init__(self, fname: str, asset: Asset) -> None:
        self.fname = fname
        self.orientation = 1
        self.title = asset.title
        self.caption = asset.caption
        self.exif_datetime = asset.taken_at.timestamp() if asset.taken_at is not None else 0.0
        self.location = _format_location(asset)
        self.people = ", ".join(asset.people) if asset.people else None


def _format_location(asset: Asset) -> str | None:
    parts = [p for p in (asset.geo.city, asset.geo.state, asset.geo.country) if p]
    return ", ".join(parts) if parts else None


# Order matches the picframe viewer's bit assignment (1, 2, 4, 8, 16, 32).
SHOW_TEXT_KEYS: tuple[str, ...] = ("title", "caption", "name", "date", "location", "folder", "people")


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


class Controller:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._stop_evt = threading.Event()
        self._force_next_evt = threading.Event()
        self._paused = False
        self._current_asset: Asset | None = None
        self._selection_mode: SelectionMode = config.selection.default_mode
        self._album_ids: list[str] = list(config.selection.album_ids)
        self._smart_query: str = config.selection.smart_query

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
        )

        # Build initial selector
        self._selector: AssetSelector = self._build_selector(self._selection_mode)
        self._prefetch = PrefetchWorker(
            self._selector,
            self._client,
            queue_size=config.selection.prefetch_count,
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

        merged_viewer = {**_VIEWER_DEFAULTS, **self._config.viewer.raw}
        self._viewer = ViewerDisplay(merged_viewer)
        self._viewer.slideshow_start()
        self._sync_to_viewer()

        if self._config.video.enabled:
            try:
                from .video.player import VideoPlayer
                self._video_player = VideoPlayer(mute=self._config.video.mute)
            except Exception as e:
                log.warning("video disabled (mpv not available): %s", e)
                self._video_player = None

        # Ping is informational only — don't block startup if Immich is slow.
        if not self._client.ping():
            log.warning("Immich ping failed at startup; will retry on first prefetch.")

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

                new_path, asset = item

                if asset.kind == AssetKind.VIDEO and self._video_player is not None:
                    self._play_video(asset)
                    next_tm = time.time() + time_delay
                    continue

                if new_path is None:
                    # Video but no player — skip.
                    continue

                self._current_asset = asset
                self._publish_state()
                pic = Pic(str(new_path), asset)
                pics_arg = [pic, None]                  # picframe slideshow_is_running shape
                loop_running, _, _ = viewer.slideshow_is_running(
                    pics_arg, time_delay=time_delay, fade_time=fade_time, paused=self._paused
                )

                # Clean up the previous slide's file once the new one has been
                # accepted by the viewer (the old texture is no longer needed).
                if current_path is not None:
                    current_path.unlink(missing_ok=True)
                current_path = new_path

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

    def _play_video(self, asset: Asset) -> None:
        if self._video_player is None:
            return
        url, headers = self._client.video_play_args(asset.id)
        self._current_asset = asset
        self._publish_state()
        end_evt = threading.Event()
        self._video_player.play(url, headers=headers, on_end=end_evt.set)
        while not end_evt.wait(timeout=0.5):
            if self._stop_evt.is_set():
                self._video_player.stop()
                return

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
        if mode not in ("random", "album", "smart", "scene"):
            raise ValueError(f"unknown selection_mode: {mode!r}")
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
    def current_scene(self) -> str | None:
        """When `selection_mode == "scene"`, the label currently driving
        selection; None otherwise."""
        if isinstance(self._selector, SceneSelector):
            return self._selector.current_scene
        return None

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
        raise ValueError(f"unknown selection_mode: {mode!r}")

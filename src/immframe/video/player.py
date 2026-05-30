"""Video playback via python-mpv.

Replaces picframe's VLC-subprocess + custom stdin protocol with a single
in-process MPV handle. Streams directly from Immich (no need to download
the whole video first) by passing `x-api-key` via MPV's `http-header-fields`
option.

Backend selection (the `vo` arg):
- 'gpu' (default) — preferred on Pi 4/5. Uses MPV's GPU video output. On a
  Pi with KMS configured, MPV will pick the DRM context automatically.
- 'sdl' / 'xv' / 'x11' — for dev hosts running X.

Threading: MPV runs its own threads. Event callbacks fire from MPV's event
thread; we just toggle the `_playing` flag and call any user callback. Keep
callbacks short — they're on MPV's hot path.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Literal

log = logging.getLogger(__name__)


VideoOutput = Literal["gpu", "sdl", "xv", "x11"]


class VideoPlayer:
    def __init__(
        self,
        *,
        mute: bool = True,
        fit: Literal["contain", "cover"] = "contain",
        vo: VideoOutput | str = "gpu",
        rotate: str = "auto",
        fullscreen: bool = False,
    ) -> None:
        # Imported lazily so the module imports cleanly on dev hosts without libmpv.
        import mpv

        self._lock = threading.Lock()
        self._playing = threading.Event()
        self._on_end: Callable[[], None] | None = None
        self._on_first_frame: Callable[[], None] | None = None

        # python-mpv constructor kwargs map underscore→dash, so `video_rotate`
        # below becomes MPV's --video-rotate option. "auto" is MPV's own
        # default (honor container rotation); we just skip setting the
        # option in that case.
        # `fullscreen` defaults to OFF. On the supported labwc kiosk the
        # compositor is configured to fullscreen every window it sees (see
        # examples/labwc/rc.xml). If MPV *also* requests fullscreen, the
        # compositor's ToggleFullscreen rule fires on an already-fullscreen
        # window and toggles it back to a small default-size window — the
        # "tiny video" symptom. Letting the compositor own fullscreen keeps
        # MPV and the pi3d window symmetric (both map windowed, both get
        # toggled to fullscreen). Set true only when running MPV under a
        # compositor that does NOT auto-fullscreen (a plain desktop / X11).
        mpv_opts: dict = dict(
            vo=vo,
            mute=mute,
            keep_open="no",
            video_unscaled="no",
            keepaspect="yes",
            panscan="1.0" if fit == "cover" else "0.0",
            fullscreen="yes" if fullscreen else "no",
            osc=False,
            input_default_bindings=False,
            input_vo_keyboard=False,
            log_handler=self._mpv_log,
        )
        if rotate != "auto":
            mpv_opts["video_rotate"] = rotate
        self._mpv = mpv.MPV(**mpv_opts)

        # Diagnostics: log what MPV reports so video issues are debuggable
        # from the systemd journal alone.
        try:
            ver = self._mpv.mpv_version
            current_vo = self._mpv.current_vo
            log.info("MPV ready: version=%r vo=%r requested-vo=%r", ver, current_vo, vo)
        except Exception as e:
            log.debug("MPV version/vo introspection failed: %s", e)

        # End-of-file: clear the flag, notify caller.
        @self._mpv.event_callback("end_file")
        def _on_end_file(_event):                       # noqa: ANN001
            self._playing.clear()
            cb = self._on_end
            if cb is not None:
                try:
                    cb()
                except Exception as e:                  # don't crash mpv's event thread
                    log.exception("on_end callback raised: %s", e)

        # First frame visible: notify caller once.
        @self._mpv.property_observer("time-pos")
        def _on_time(_name, value):                     # noqa: ANN001
            if value is not None and value > 0 and not self._playing.is_set():
                self._playing.set()
                cb = self._on_first_frame
                if cb is not None:
                    self._on_first_frame = None
                    try:
                        cb()
                    except Exception as e:
                        log.exception("on_first_frame callback raised: %s", e)

    # ── Diagnostics ─────────────────────────────────────────────────────
    @staticmethod
    def _mpv_log(level: str, prefix: str, message: str) -> None:
        """MPV pipes its internal logs through this. We surface fatal/error
        levels at WARNING and the rest at DEBUG so noisy MPV chatter
        doesn't drown out our own logs by default."""
        msg = f"mpv[{prefix}]: {message.rstrip()}"
        if level in ("fatal", "error"):
            log.warning(msg)
        elif level == "warn":
            log.info(msg)
        else:
            log.debug(msg)

    # ── Playback ────────────────────────────────────────────────────────
    def play(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        on_first_frame: Callable[[], None] | None = None,
        on_end: Callable[[], None] | None = None,
    ) -> None:
        """Start playback. Returns immediately; observe `on_first_frame` /
        `on_end` for state changes."""
        with self._lock:
            self._on_first_frame = on_first_frame
            self._on_end = on_end
            self._playing.clear()
            if headers:
                hdr_str = ",".join(f"{k}: {v}" for k, v in headers.items())
                self._mpv["http-header-fields"] = hdr_str
            else:
                self._mpv["http-header-fields"] = ""
            self._mpv.command("loadfile", url, "replace")

    def stop(self) -> None:
        with self._lock:
            try:
                self._mpv.command("stop")
            except Exception as e:                      # mpv was already destroyed
                log.debug("stop() during shutdown: %s", e)
            self._playing.clear()

    def pause(self, paused: bool) -> None:
        with self._lock:
            self._mpv["pause"] = paused

    # ── State ───────────────────────────────────────────────────────────
    @property
    def is_playing(self) -> bool:
        return self._playing.is_set()

    # ── Lifecycle ───────────────────────────────────────────────────────
    def close(self) -> None:
        with self._lock:
            try:
                self._mpv.terminate()
            except Exception as e:
                log.debug("terminate during close: %s", e)

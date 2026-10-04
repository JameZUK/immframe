"""Live / motion photo playback styles.

A live photo (Apple Live Photo, Samsung / Google Motion Photo — the HEIC or
JPEG with a 1.5–4 s clip attached) is shown as the still first, then its
motion clip plays, then the still comes back. This module turns the
user's settings into an MPV playback recipe for the clip:

    once     play forward once                        (the classic)
    loop     play forward, again and again
    bounce   forward then backward, repeated           ("boomerang")
    reverse  play backward once                        (rewind effect)
    still    never play the motion — just the photo

Timing knobs (all in `video:` config, adjustable live from the dashboard):

    live_photo_order     photo_first = the photo, fully faded in, for hold_s,
                         then the clip; video_first = the clip straight away,
                         then the photo for the rest of the slide
    live_photo_hold_s    photo_first: how long the photo is fully visible
                         before the clip starts (counted after the crossfade)
    live_photo_play_s    how long loop / bounce keep going (once / reverse: a
                         cap on the clip's own length; 0 = no cap)
    live_photo_repeats   loop / bounce: stop after this many cycles instead
                         of after play_s (0 = use play_s)
    live_photo_speed     playback speed (0.25 = quarter speed slow-mo … 4)
    live_photo_pause_s   freeze on the last frame for this long at each
                         loop point / bounce turnaround
    live_photo_after     "still" = back to the photo for the rest of the
                         slide; "next" = move on as soon as the clip ends

Reverse playback (bounce, reverse) is done with ffmpeg's `reverse` filter,
which buffers the frames it reverses; the clip is trimmed to
`MAX_REVERSE_S` first so an unexpectedly long clip can't exhaust memory
(a 3.7 s 960x720 clip costs ~100 MB on the Pi).
"""
from __future__ import annotations

from dataclasses import dataclass

LIVE_MODES: tuple[str, ...] = ("once", "loop", "bounce", "reverse", "still")
LIVE_AFTER: tuple[str, ...] = ("still", "next")
LIVE_ORDER: tuple[str, ...] = ("photo_first", "video_first")
MAX_REVERSE_S = 6.0
SPEED_RANGE = (0.25, 4.0)


@dataclass
class LiveSettings:
    mode: str = "once"
    hold_s: float = 1.0
    play_s: float = 6.0
    repeats: int = 0
    speed: float = 1.0
    pause_s: float = 0.0
    after: str = "still"
    hwdec: str = "no"
    order: str = "photo_first"

    def validated(self) -> "LiveSettings":
        """A clamped copy; raises ValueError for an unknown mode / after."""
        if self.mode not in LIVE_MODES:
            raise ValueError(f"live_photo_mode must be one of {LIVE_MODES}; got {self.mode!r}")
        if self.order not in LIVE_ORDER:
            raise ValueError(f"live_photo_order must be one of {LIVE_ORDER}; got {self.order!r}")
        if self.after not in LIVE_AFTER:
            raise ValueError(f"live_photo_after must be one of {LIVE_AFTER}; got {self.after!r}")
        lo, hi = SPEED_RANGE
        return LiveSettings(
            mode=self.mode,
            hold_s=min(max(0.0, float(self.hold_s)), 60.0),
            play_s=min(max(0.0, float(self.play_s)), 300.0),
            repeats=min(max(0, int(self.repeats)), 100),
            speed=min(max(lo, float(self.speed)), hi),
            pause_s=min(max(0.0, float(self.pause_s)), 10.0),
            after=self.after,
            hwdec=str(self.hwdec or "no"),
            order=self.order,
        )


@dataclass(frozen=True)
class PlaybackRecipe:
    """What to hand MPV for one clip."""
    vf: str              # "" = no filter
    loop: str            # MPV loop-file: "no" | "inf" | "<n extra plays>"
    speed: float
    max_s: float | None  # stop after this long (None = until the clip ends)
    audio: bool          # False when reversed (backwards audio is just noise)


def recipe(s: LiveSettings) -> PlaybackRecipe | None:
    """MPV recipe for the clip, or None when the clip shouldn't play."""
    s = s.validated()
    if s.mode == "still":
        return None
    hold = f"tpad=stop_mode=clone:stop_duration={s.pause_s:g}" if s.pause_s > 0 else None
    trim = f"trim=end={MAX_REVERSE_S:g},setpts=PTS-STARTPTS"

    if s.mode == "bounce":
        fwd = f"[a]{hold}[f];" if hold else ""
        back = f"[r]{hold}[b];" if hold else ""
        a_out = "[f]" if hold else "[a]"
        r_out = "[b]" if hold else "[r]"
        graph = (f"{trim},split[a][c];[c]reverse[r];{fwd}{back}"
                 f"{a_out}{r_out}concat=n=2:v=1:a=0")
        vf = f"lavfi=[{graph}]"
    elif s.mode == "reverse":
        vf = f"lavfi=[{trim},reverse]"
    elif s.mode == "loop" and hold:
        vf = f"lavfi=[{hold}]"
    else:
        vf = ""

    repeating = s.mode in ("loop", "bounce")
    if repeating and s.repeats > 0:
        loop = str(s.repeats - 1) if s.repeats > 1 else "no"   # loop-file=N → N extra plays
        max_s = None
    elif repeating:
        loop = "inf"
        max_s = s.play_s or None
    else:
        loop = "no"
        max_s = s.play_s or None
    return PlaybackRecipe(vf=vf, loop=loop, speed=s.speed, max_s=max_s,
                          audio=s.mode not in ("bounce", "reverse"))


def live_settings_from(video) -> LiveSettings:
    """LiveSettings from a VideoConfig's live_photo_* fields."""
    return LiveSettings(
        mode=video.live_photo_mode, hold_s=video.live_photo_hold_s,
        play_s=video.live_photo_play_s, repeats=video.live_photo_repeats,
        speed=video.live_photo_speed, pause_s=video.live_photo_pause_s,
        after=video.live_photo_after, hwdec=video.live_photo_hwdec,
        order=video.live_photo_order,
    )

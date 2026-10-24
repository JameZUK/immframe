"""Burst shots: one press of a phone's shutter that Immich keeps as many
separate assets (`2021-06-15_17.20.04.jpg`, `…-1.jpg` … `…-18.jpg`).

They differ byte-for-byte, so Immich (and its duplicate detection, only
partly) treats them as different photos. For the frame they're one photo:

- blocking one blocks the rest of its burst (controller.hide_asset);
- once a shot from a burst is queued, its burst-mates are skipped for a
  while (`BurstFilter`), so a random draw doesn't show the same moment
  again and again — a 19-shot burst is otherwise 19x as likely to come up.

Two assets are the same burst when both have a capture time, are the same
kind, have the same dimensions (either orientation) and were taken within
`window_s` seconds of each other (default 3: a phone's burst runs over
a second or two, and a shot just before it looks the same on the frame).
"""
from __future__ import annotations

import threading
import time
from collections import deque

from .immich.models import Asset


def same_burst(a: Asset, b: Asset, window_s: float) -> bool:
    if window_s <= 0 or a.id == b.id or a.taken_at is None or b.taken_at is None:
        return False
    if a.kind != b.kind or {a.width, a.height} != {b.width, b.height}:
        return False
    return abs((a.taken_at - b.taken_at).total_seconds()) <= window_s


class BurstFilter:
    """Remembers recently queued shots; `is_repeat` is True for a burst-mate
    of one queued within the last `cooldown_s`. Thread-safe."""

    def __init__(self, window_s: float, cooldown_s: float) -> None:
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self._recent: deque[tuple[float, Asset]] = deque()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.window_s > 0 and self.cooldown_s > 0

    def _expire(self, now: float) -> None:
        while self._recent and now - self._recent[0][0] > self.cooldown_s:
            self._recent.popleft()

    def is_repeat(self, asset: Asset) -> bool:
        if not self.enabled or asset.taken_at is None:
            return False
        with self._lock:
            self._expire(time.time())
            return any(same_burst(asset, seen, self.window_s) for _, seen in self._recent)

    def note(self, asset: Asset) -> None:
        if not self.enabled or asset.taken_at is None:
            return
        with self._lock:
            now = time.time()
            self._expire(now)
            self._recent.append((now, asset))

    def thin(self, batch: list[Asset]) -> list[Asset]:
        """The batch minus burst repeats (of recent shots or of each other).
        If that would leave nothing — a selection that *is* one burst — the
        first shot is kept so the frame still has something to show."""
        if not self.enabled:
            return batch
        out: list[Asset] = []
        for a in batch:
            if self.is_repeat(a) or any(same_burst(a, b, self.window_s) for b in out):
                continue
            out.append(a)
        return out or batch[:1]

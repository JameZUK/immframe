"""Full-screen status slide for when the frame can't load photos.

Rendered with Pillow at the display's resolution and shown through the
normal viewer path, so it crossfades in and out like a photo:

    Can't load photos from Immich
    Retrying…
    photos.jamez.me.uk · not responding for 12 min · Immich didn't answer in time
    The frame will carry on by itself as soon as Immich is back.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

_FONT_DIR = Path(__file__).parent / "viewer" / "data" / "fonts"
BG_TOP = (14, 16, 24)
BG_BOTTOM = (26, 30, 44)
ACCENT = (124, 140, 255)


def describe_duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} s"
    m = round(s / 60)
    if m < 90:
        return f"{m} min"
    return f"{s / 3600:.1f} h"


def status_lines(immich_url: str, health: dict, *, now: float | None = None) -> tuple[str, str, str, str]:
    """(title, subtitle, detail, hint) for the screen and the dashboard."""
    now = time.time() if now is None else now
    host = urlsplit(immich_url).hostname or immich_url
    since = health.get("failing_since")
    err = health.get("last_error")
    if since is not None:
        detail = f"{host} · not responding for {describe_duration(now - since)}"
        if err:
            detail += f" · {err}"
        title = "Can't load photos from Immich"
    else:
        detail = f"{host} · waiting for the first photos"
        title = "Loading photos from Immich"
    return title, "Retrying…", detail, "The frame will carry on by itself as soon as Immich is back."


def render(dest: Path, size: tuple[int, int], lines: tuple[str, str, str, str]) -> bool:
    """Draw the status slide to `dest` (JPEG). Returns False on failure."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:                                # pragma: no cover
        return False
    w, h = max(320, int(size[0])), max(180, int(size[1]))
    img = Image.new("RGB", (w, h), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(h):                                 # soft vertical gradient
        t = y / max(1, h - 1)
        draw.line([(0, y), (w, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))

    def font(name: str, px: int):
        try:
            return ImageFont.truetype(str(_FONT_DIR / name), px)
        except OSError:
            return ImageFont.load_default()

    title, sub, detail, hint = lines
    f_title = font("NotoSans-Bold.ttf", round(h * 0.055))
    f_sub = font("NotoSans-Regular.ttf", round(h * 0.035))
    f_small = font("NotoSans-Regular.ttf", round(h * 0.025))

    # A simple "photo frame" mark with a pulsing-dot feel (static: three dots).
    cx, cy = w // 2, round(h * 0.33)
    r = round(h * 0.06)
    draw.rounded_rectangle([cx - r, cy - round(r * 0.8), cx + r, cy + round(r * 0.8)],
                           radius=round(r * 0.25), outline=ACCENT, width=max(3, r // 10))
    for i, alpha in enumerate((255, 170, 90)):
        dx = (i - 1) * round(r * 0.45)
        rr = max(3, r // 9)
        col = tuple(round(c * alpha / 255 + BG_TOP[k] * (1 - alpha / 255)) for k, c in enumerate(ACCENT))
        draw.ellipse([cx + dx - rr, cy - rr, cx + dx + rr, cy + rr], fill=col)

    def centred(text: str, y: int, f, fill) -> int:
        tw = draw.textlength(text, font=f)
        while tw > w * 0.9 and len(text) > 4:          # trim overly long details
            text = text[:-2]
            tw = draw.textlength(text + "…", font=f)
            if tw <= w * 0.9:
                text += "…"
                break
        draw.text(((w - tw) / 2, y), text, font=f, fill=fill)
        return y + round(f.size * 1.5) if hasattr(f, "size") else y + 20

    y = round(h * 0.48)
    y = centred(title, y, f_title, (236, 239, 245))
    y = centred(sub, y, f_sub, ACCENT)
    y += round(h * 0.02)
    y = centred(detail, y, f_small, (160, 168, 186))
    centred(hint, y, f_small, (120, 128, 146))
    tmp = dest.with_name(dest.name + ".part")
    try:
        img.save(tmp, "JPEG", quality=90)
        tmp.replace(dest)
    except OSError as e:
        log.warning("status screen save failed: %s", e)
        return False
    return True

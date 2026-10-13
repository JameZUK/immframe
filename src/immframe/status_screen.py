"""Full-screen status slide for when the frame can't load photos.

Rendered with Pillow at the display's resolution and shown through the
normal viewer path, so it crossfades in and out like a photo:

    Can't load photos from Immich
    Retrying…
    photos.example.org · not responding for 12 min · Immich didn't answer in time
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


def status_lines(
    immich_url: str, health: dict, *, now: float | None = None,
    first: bool = True, empty: bool = False,
) -> tuple[str, str, str, str]:
    """(title, subtitle, detail, hint) for the screen.

    Immich failing wins; otherwise Immich is fine and either the selection
    matched nothing (`empty`) or photos are still downloading — `first`
    when none has been shown since start-up."""
    now = time.time() if now is None else now
    host = urlsplit(immich_url).hostname or immich_url
    since = health.get("failing_since")
    err = health.get("last_error")
    if since is not None:
        verb = "failing" if err and "API key" in err else "not responding"
        detail = f"{host} · {verb} for {describe_duration(now - since)}"
        if err:
            detail += f" · {err}"
        if err and "API key" in err:
            hint = "Check the frame's Immich API key — it carries on by itself once Immich accepts it."
        else:
            hint = "The frame will carry on by itself as soon as Immich is back."
        return "Can't load photos from Immich", "Retrying…", detail, hint
    if empty:
        return ("No photos to show", "Checking again…",
                f"{host} · nothing matches the current selection",
                "Pick a different selection on the dashboard — new matches are picked up by themselves.")
    detail = f"{host} · " + ("waiting for the first photos" if first else "the next photo is taking a while")
    return ("Loading photos from Immich", "Please wait…", detail,
            "The next photo will appear as soon as it has downloaded.")


def render(dest: Path, size: tuple[int, int], lines: tuple[str, str, str, str]) -> bool:
    """Draw the status slide to `dest` (JPEG). Returns False on failure."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:                                # pragma: no cover
        return False
    w, h = max(320, int(size[0])), max(180, int(size[1]))
    # Soft vertical gradient: one column, stretched (fast even at 4K).
    col = Image.new("RGB", (1, h))
    col.putdata([tuple(round(a + (b - a) * y / max(1, h - 1)) for a, b in zip(BG_TOP, BG_BOTTOM))
                 for y in range(h)])
    img = col.resize((w, h), Image.NEAREST)
    draw = ImageDraw.Draw(img)

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
        if draw.textlength(text, font=f) > w * 0.9:    # trim overly long details
            while len(text) > 1 and draw.textlength(text + "…", font=f) > w * 0.9:
                text = text[:-1]
            text = text.rstrip() + "…"
        tw = draw.textlength(text, font=f)
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

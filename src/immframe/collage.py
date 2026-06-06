"""Collage composition — tile K photos into one image.

Collage is a *presentation* layer, orthogonal to selection mode: the prefetch
worker pulls K assets from whatever selector is active, downloads them, and
composites them here into a single JPEG that flows through the normal render
path (so it gets mat / blur-edges / crossfade / overlay for free, and the
viewer never knows it's a collage).

This module has two halves:

1. **Pure geometry** (`grid_rects`, `golden_rects`, `choose_layout`) — no PIL,
   no I/O, trivially unit-testable. They turn a tile count + canvas size into a
   list of `Rect`s.
2. **Compositing** (`render_collage`) — lazily imports PIL, loads each source,
   cover-crops (or letterboxes) it into its rect, and writes the JPEG.

Two layout algorithms:
- ``grid``         — uniform rows×cols (cols ≈ √K), clean and predictable.
- ``golden_ratio`` — recursive φ-weighted split (0.618 : 0.382), always cutting
  the longer side, giving the organic magazine/Fibonacci-spiral look.

``layout: auto`` picks between them from the tile count and orientation mix.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 1/φ. The placed image in each golden split takes the *minor* section
# (1 - INV_PHI = 0.382) and we keep subdividing the major remainder, so the
# final tile is the largest — the recognisable golden-spiral hierarchy.
INV_PHI = 0.6180339887498949
_MINOR = 1.0 - INV_PHI  # 0.381966…

# Synthetic-asset id prefix for collages. The id is NOT a real Immich asset,
# so the HTTP image proxy must serve the local composite instead (see
# /api/current_image) rather than forwarding it to Immich.
COLLAGE_ID_PREFIX = "collage-"


def is_collage_id(asset_id: str) -> bool:
    return bool(asset_id) and asset_id.startswith(COLLAGE_ID_PREFIX)


# NotoSans, vendored with the pi3d viewer — reused for per-tile captions.
_FONT_PATH = Path(__file__).parent / "viewer" / "data" / "fonts" / "NotoSans-Regular.ttf"

# Fields a per-tile caption can show (mirrors the viewer's overlay keys).
TILE_TEXT_KEYS = ("caption", "date", "location", "name", "people", "tags")


def asset_caption(asset, fields: list[str], *, date_fmt: str = "%b %Y") -> str:
    """Build a one-line per-tile caption from an Asset and a list of field
    keys (in order). Skips fields with no value; joins with ' · '."""
    parts: list[str] = []
    for f in fields:
        if f == "caption" and asset.caption:
            parts.append(asset.caption)
        elif f == "date" and asset.taken_at is not None:
            parts.append(asset.taken_at.strftime(date_fmt))
        elif f == "location":
            loc = ", ".join(p for p in (asset.geo.city, asset.geo.state, asset.geo.country) if p)
            if loc:
                parts.append(loc)
        elif f == "name" and asset.original_file_name:
            parts.append(asset.original_file_name)
        elif f == "people" and asset.people:
            parts.append(", ".join(asset.people))
        elif f == "tags" and asset.tag_names:
            parts.append(", ".join(asset.tag_names))
    return " · ".join(parts)


@dataclass(frozen=True)
class Rect:
    """A tile rectangle in pixels (floats; rounded at paste time)."""
    x: float
    y: float
    w: float
    h: float


# ── Pure geometry ───────────────────────────────────────────────────────────
def grid_rects(n: int, w: float, h: float, gap: float) -> list[Rect]:
    """Uniform grid. `gap` is used both as the outer margin and the gutter, so
    tiles sit inside a clean border. The final (partial) row's tiles widen to
    fill the canvas width."""
    if n <= 0:
        return []
    rows = max(1, round(math.sqrt(n)))
    cols = math.ceil(n / rows)
    cell_h = (h - gap * (rows + 1)) / rows
    rects: list[Rect] = []
    placed = 0
    for r in range(rows):
        remaining = n - placed
        if remaining <= 0:
            break
        tiles_in_row = min(cols, remaining)
        cell_w = (w - gap * (tiles_in_row + 1)) / tiles_in_row
        y = gap + r * (cell_h + gap)
        for c in range(tiles_in_row):
            x = gap + c * (cell_w + gap)
            rects.append(Rect(x, y, cell_w, cell_h))
            placed += 1
    return rects


def golden_rects(n: int, w: float, h: float, gap: float) -> list[Rect]:
    """φ-weighted binary split. `gap` is the outer margin and the gutter
    between each split. Always cuts the rectangle's longer side so no tile
    degenerates into a sliver."""
    if n <= 0:
        return []
    x, y = gap, gap
    cw, ch = w - 2 * gap, h - 2 * gap
    rects: list[Rect] = []
    for _ in range(n - 1):
        if cw >= ch:                                   # split left | right
            minor = (cw - gap) * _MINOR
            rects.append(Rect(x, y, minor, ch))
            x += minor + gap
            cw -= minor + gap
        else:                                          # split top | bottom
            minor = (ch - gap) * _MINOR
            rects.append(Rect(x, y, cw, minor))
            y += minor + gap
            ch -= minor + gap
    rects.append(Rect(x, y, cw, ch))                   # remainder = hero tile
    return rects


def is_perfect_square(n: int) -> bool:
    if n < 0:
        return False
    r = int(round(math.sqrt(n)))
    return r * r == n


def choose_layout(configured: str, n: int, is_portrait: list[bool]) -> str:
    """Resolve ``auto`` to a concrete layout from the tile count + orientation
    mix; pass an explicit ``grid``/``golden_ratio`` through unchanged.

    Auto rule: K≤3 → golden; perfect square (4, 9, …) → grid; mixed
    portrait/landscape → golden; otherwise grid.
    """
    if configured in ("grid", "golden_ratio"):
        return configured
    if n <= 3:
        return "golden_ratio"
    if is_perfect_square(n):
        return "grid"
    mixed = bool(is_portrait) and any(is_portrait) and not all(is_portrait)
    if mixed:
        return "golden_ratio"
    return "grid"


def layout_rects(layout: str, n: int, w: float, h: float, gap: float) -> list[Rect]:
    if layout == "grid":
        return grid_rects(n, w, h, gap)
    return golden_rects(n, w, h, gap)


# ── Colour helper (shared with config validation) ───────────────────────────
def parse_hex_color(s: str) -> tuple[int, int, int]:
    """Parse ``#rgb`` / ``#rrggbb`` (with or without ``#``) to an RGB tuple."""
    t = str(s).strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    if len(t) != 6:
        raise ValueError(f"invalid hex color {s!r} (expected #rgb or #rrggbb)")
    try:
        return (int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16))
    except ValueError as e:
        raise ValueError(f"invalid hex color {s!r}: {e}") from e


# ── Compositing ─────────────────────────────────────────────────────────────
def _get_font(size: int, cache: dict):
    if size in cache:
        return cache[size]
    from PIL import ImageFont
    try:
        font = ImageFont.truetype(str(_FONT_PATH), size)
    except Exception as e:                             # noqa: BLE001
        log.debug("collage caption font load failed: %s", e)
        font = None
    cache[size] = font
    return font


def _truncate(draw, text: str, font, max_w: float) -> str:
    """Trim `text` (appending an ellipsis) until it fits `max_w` pixels."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return (text + "…") if text else ""


def _draw_tile_caption(draw, text: str, rect: Rect, base_fs: int, cache: dict) -> None:
    fs = max(12, min(base_fs, round(rect.h * 0.16)))   # shrink for small tiles
    font = _get_font(fs, cache)
    if font is None:
        return
    margin = max(4, fs // 3)
    text = _truncate(draw, text, font, rect.w - 2 * margin)
    if not text:
        return
    x = round(rect.x) + margin
    y = round(rect.y + rect.h) - fs - margin
    # White text with a black outline reads on any photo, no backing strip.
    draw.text(
        (x, y), text, font=font, fill=(255, 255, 255),
        stroke_width=max(1, fs // 12), stroke_fill=(0, 0, 0),
    )


def render_collage(
    image_paths: list[Path],
    is_portrait: list[bool],
    dest: Path,
    *,
    canvas_size: tuple[int, int],
    gap: int,
    background: str,
    fit: str,
    layout: str,
    captions: list[str] | None = None,
) -> bool:
    """Composite `image_paths` into one JPEG at `dest`. Returns True on success.

    A tile that fails to load is left as background rather than aborting the
    whole collage. When `captions` is given (one string per image), each tile
    gets a small outlined caption in its bottom-left corner. Writes atomically.
    """
    from PIL import Image, ImageOps, ImageDraw         # lazy: keep geometry PIL-free

    n = len(image_paths)
    if n < 2:
        return False
    chosen = choose_layout(layout, n, is_portrait)
    rects = layout_rects(chosen, n, canvas_size[0], canvas_size[1], gap)
    canvas = Image.new("RGB", canvas_size, parse_hex_color(background))

    for path, rect in zip(image_paths, rects):
        cell = (max(1, round(rect.w)), max(1, round(rect.h)))
        ox, oy = round(rect.x), round(rect.y)
        try:
            with Image.open(path) as im:
                im.draft("RGB", cell)                  # fast JPEG downscale near target size
                im = ImageOps.exif_transpose(im) or im
                im = im.convert("RGB")
                if fit == "cover":
                    tile = ImageOps.fit(im, cell, method=Image.BICUBIC, centering=(0.5, 0.5))
                    canvas.paste(tile, (ox, oy))
                else:                                  # contain: letterbox within the cell
                    thumb = im.copy()
                    thumb.thumbnail(cell, Image.BICUBIC)
                    canvas.paste(
                        thumb,
                        (ox + (cell[0] - thumb.width) // 2, oy + (cell[1] - thumb.height) // 2),
                    )
        except Exception as e:                         # noqa: BLE001 — one bad tile ≠ dead collage
            log.warning("collage tile load failed for %s: %s", path, e)

    if captions and any(captions):
        draw = ImageDraw.Draw(canvas)
        base_fs = max(14, round(canvas_size[1] * 0.024))
        font_cache: dict = {}
        for cap, rect in zip(captions, rects):
            if cap:
                _draw_tile_caption(draw, cap, rect, base_fs, font_cache)

    tmp = dest.with_name(dest.name + ".part")
    try:
        canvas.save(tmp, "JPEG", quality=90)
        os.replace(tmp, dest)
    except OSError as e:
        log.warning("collage save failed: %s", e)
        Path(tmp).unlink(missing_ok=True)
        return False
    log.info("collage: %d tiles, layout=%s, %dx%d", n, chosen, canvas_size[0], canvas_size[1])
    return True


def make_collage_asset(stem: str, label: str, count: int):
    """A synthetic `Asset` standing in for the composited collage so it flows
    through the render path and surfaces a generic label in overlay / state."""
    from .immich.models import Asset, AssetKind, GeoInfo
    return Asset(
        id=stem,
        kind=AssetKind.IMAGE,
        original_file_name=label,
        mime_type="image/jpeg",
        width=0,
        height=0,
        taken_at=None,
        geo=GeoInfo(None, None, None, None, None),
        camera_make=None,
        camera_model=None,
        title=None,
        caption=label,
        tag_names=(),
        people=(),
        favorite=False,
        live_photo_video_id=None,
    )

from __future__ import annotations

from pathlib import Path

import pytest

from datetime import datetime

from immframe.collage import (
    Rect,
    asset_caption,
    choose_layout,
    golden_rects,
    grid_rects,
    is_collage_id,
    is_perfect_square,
    layout_rects,
    make_collage_asset,
    parse_hex_color,
    render_collage,
)


def _capasset(**kw):
    from immframe.immich.models import Asset, AssetKind, GeoInfo
    d = dict(
        id="x", kind=AssetKind.IMAGE, original_file_name="IMG_1.jpg",
        mime_type="image/jpeg", width=100, height=100, taken_at=None,
        geo=GeoInfo(None, None, None, None, None), camera_make=None, camera_model=None,
        title=None, caption=None, tag_names=(), people=(), favorite=False,
        live_photo_video_id=None,
    )
    d.update(kw)
    return Asset(**d)

_EPS = 1e-6


def _overlap(a: Rect, b: Rect) -> bool:
    return (
        a.x < b.x + b.w - _EPS
        and b.x < a.x + a.w - _EPS
        and a.y < b.y + b.h - _EPS
        and b.y < a.y + a.h - _EPS
    )


def _assert_valid(rects: list[Rect], n: int, w: float, h: float) -> None:
    assert len(rects) == n
    for r in rects:
        assert r.w > 0 and r.h > 0
        assert r.x >= -_EPS and r.y >= -_EPS
        assert r.x + r.w <= w + _EPS
        assert r.y + r.h <= h + _EPS
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            assert not _overlap(rects[i], rects[j]), f"tiles {i},{j} overlap"


# ── Geometry: grid ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 8, 9, 12])
def test_grid_rects_valid(n: int):
    _assert_valid(grid_rects(n, 1920, 1080, 8), n, 1920, 1080)


def test_grid_square_count_is_square_grid():
    # 4 → 2x2: two distinct x's, two distinct y's
    rects = grid_rects(4, 1000, 1000, 10)
    xs = sorted({round(r.x) for r in rects})
    ys = sorted({round(r.y) for r in rects})
    assert len(xs) == 2 and len(ys) == 2


def test_grid_zero_is_empty():
    assert grid_rects(0, 100, 100, 4) == []


# ── Geometry: golden ratio ──────────────────────────────────────────────────
@pytest.mark.parametrize("n", [2, 3, 4, 5, 6])
def test_golden_rects_valid(n: int):
    _assert_valid(golden_rects(n, 1920, 1080, 8), n, 1920, 1080)


def test_golden_tiles_vary_in_size():
    # The defining trait vs a grid: φ splits produce clearly non-uniform tiles.
    rects = golden_rects(4, 1600, 1000, 0)
    areas = [r.w * r.h for r in rects]
    assert max(areas) > min(areas) * 1.5


def test_golden_no_gap_tiles_pack_tightly():
    rects = golden_rects(3, 100, 100, 0)
    total = sum(r.w * r.h for r in rects)
    assert total == pytest.approx(100 * 100, rel=1e-9)


# ── Auto layout selection ───────────────────────────────────────────────────
def test_is_perfect_square():
    assert is_perfect_square(4) and is_perfect_square(9) and is_perfect_square(1)
    assert not is_perfect_square(5) and not is_perfect_square(6)


def test_choose_layout_explicit_passthrough():
    assert choose_layout("grid", 3, []) == "grid"
    assert choose_layout("golden_ratio", 9, []) == "golden_ratio"


def test_choose_layout_small_count_is_golden():
    assert choose_layout("auto", 2, [True, False]) == "golden_ratio"
    assert choose_layout("auto", 3, [False, False, False]) == "golden_ratio"


def test_choose_layout_square_count_is_grid():
    assert choose_layout("auto", 4, [False, False, False, False]) == "grid"
    assert choose_layout("auto", 9, [False] * 9) == "grid"


def test_choose_layout_mixed_orientation_is_golden():
    # 6 tiles, not square, mixed portrait/landscape → golden
    assert choose_layout("auto", 6, [True, False, True, False, True, False]) == "golden_ratio"


def test_choose_layout_uniform_orientation_is_grid():
    # 6 tiles, not square, all landscape → grid
    assert choose_layout("auto", 6, [False] * 6) == "grid"


def test_layout_rects_dispatch():
    assert len(layout_rects("grid", 5, 800, 600, 4)) == 5
    assert len(layout_rects("golden_ratio", 5, 800, 600, 4)) == 5


# ── Colour parsing ──────────────────────────────────────────────────────────
def test_parse_hex_color():
    assert parse_hex_color("#ff8800") == (255, 136, 0)
    assert parse_hex_color("fff") == (255, 255, 255)
    assert parse_hex_color("#000") == (0, 0, 0)


def test_parse_hex_color_rejects_bad():
    with pytest.raises(ValueError):
        parse_hex_color("nope")
    with pytest.raises(ValueError):
        parse_hex_color("#12345")


# ── Compositing (PIL) ───────────────────────────────────────────────────────
def _make_jpeg(path: Path, size=(40, 30), color=(120, 60, 30)) -> Path:
    from PIL import Image
    Image.new("RGB", size, color).save(path, "JPEG")
    return path


@pytest.mark.parametrize("layout", ["grid", "golden_ratio", "auto"])
@pytest.mark.parametrize("fit", ["cover", "contain"])
def test_render_collage_writes_canvas_sized_jpeg(tmp_path: Path, layout: str, fit: str):
    from PIL import Image
    paths = [_make_jpeg(tmp_path / f"s{i}.jpg") for i in range(4)]
    dest = tmp_path / "out.jpg"
    ok = render_collage(
        paths, [False, True, False, True], dest,
        canvas_size=(200, 150), gap=4, background="#101018", fit=fit, layout=layout,
    )
    assert ok
    assert dest.exists()
    with Image.open(dest) as im:
        assert im.size == (200, 150)


def test_render_collage_tolerates_one_bad_tile(tmp_path: Path):
    good = [_make_jpeg(tmp_path / f"g{i}.jpg") for i in range(2)]
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a jpeg")
    dest = tmp_path / "out.jpg"
    ok = render_collage(
        [good[0], bad, good[1]], [False, False, False], dest,
        canvas_size=(120, 90), gap=2, background="#000000", fit="cover", layout="grid",
    )
    assert ok and dest.exists()


def test_render_collage_needs_two(tmp_path: Path):
    one = [_make_jpeg(tmp_path / "only.jpg")]
    dest = tmp_path / "out.jpg"
    ok = render_collage(
        one, [False], dest,
        canvas_size=(100, 100), gap=2, background="#000000", fit="cover", layout="grid",
    )
    assert ok is False
    assert not dest.exists()


# ── Per-tile captions ───────────────────────────────────────────────────────
def test_asset_caption_date_and_location():
    from immframe.immich.models import GeoInfo
    a = _capasset(taken_at=datetime(2023, 6, 15),
                  geo=GeoInfo(None, None, "Paris", None, "France"))
    assert asset_caption(a, ["date", "location"]) == "Jun 2023 · Paris, France"


def test_asset_caption_skips_absent_fields():
    a = _capasset(caption="Birthday")
    # location has no value → skipped, only caption remains
    assert asset_caption(a, ["caption", "location"]) == "Birthday"


def test_asset_caption_empty_when_nothing_available():
    assert asset_caption(_capasset(), ["date", "location"]) == ""


def test_asset_caption_people_and_name():
    a = _capasset(people=("Alice", "Bob"), original_file_name="DSC_9.jpg")
    assert asset_caption(a, ["people"]) == "Alice, Bob"
    assert asset_caption(a, ["name"]) == "DSC_9.jpg"


def test_render_collage_with_captions(tmp_path: Path):
    from PIL import Image
    paths = [_make_jpeg(tmp_path / f"s{i}.jpg") for i in range(3)]
    dest = tmp_path / "out.jpg"
    ok = render_collage(
        paths, [False, False, True], dest,
        canvas_size=(300, 200), gap=4, background="#000000", fit="cover",
        layout="grid", captions=["Jun 2023 · Paris", "", "Alice"],
    )
    assert ok and dest.exists()
    with Image.open(dest) as im:
        assert im.size == (300, 200)


# ── Synthetic asset ─────────────────────────────────────────────────────────
def test_make_collage_asset():
    from immframe.immich.models import AssetKind
    a = make_collage_asset("collage-7", "Random • 4 photos", 4)
    assert a.id == "collage-7"
    assert a.kind == AssetKind.IMAGE
    assert a.caption == "Random • 4 photos"
    assert a.original_file_name == "Random • 4 photos"
    assert a.live_photo_video_id is None
    assert is_collage_id(a.id) is True
    assert is_collage_id("abc12345-real-asset") is False

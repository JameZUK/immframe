from __future__ import annotations

from pathlib import Path

from immframe.status_screen import describe_duration, render, status_lines


def test_lines_when_failing_and_when_just_waiting():
    t, sub, detail, hint = status_lines("https://photos.example.org", {"failing_since": 1000.0, "last_error": "Immich didn't answer in time"}, now=1000.0 + 13 * 60)
    assert t == "Can't load photos from Immich" and sub == "Retrying…"
    assert detail == "photos.example.org · not responding for 13 min · Immich didn't answer in time"
    t, _, detail, _ = status_lines("https://photos.example.org", {"failing_since": None}, now=5.0)
    assert t == "Loading photos from Immich" and "waiting for the first photos" in detail


def test_describe_duration():
    assert describe_duration(42) == "42 s"
    assert describe_duration(600) == "10 min"
    assert describe_duration(3 * 3600) == "3.0 h"


def test_render_writes_a_jpeg_at_display_size(tmp_path: Path):
    from PIL import Image
    dest = tmp_path / "s.jpg"
    assert render(dest, (640, 360), status_lines("https://x", {"failing_since": None}))
    with Image.open(dest) as im:
        assert im.size == (640, 360) and im.format == "JPEG"

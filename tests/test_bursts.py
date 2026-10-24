from __future__ import annotations

from datetime import datetime, timedelta, timezone

from immframe.bursts import BurstFilter, same_burst
from immframe.immich.models import Asset, AssetKind, GeoInfo

T0 = datetime(2021, 6, 15, 17, 20, 4, tzinfo=timezone.utc)


def _a(aid, *, dt=0.0, w=4608, h=2304, kind=AssetKind.IMAGE, taken=True):
    return Asset(
        id=aid, kind=kind, original_file_name=f"{aid}.jpg", mime_type="image/jpeg",
        width=w, height=h, taken_at=T0 + timedelta(seconds=dt) if taken else None,
        geo=GeoInfo(None, None, None, None, None), camera_make=None, camera_model=None,
        title=None, caption=None, tag_names=(), people=(), favorite=False, live_photo_video_id=None,
    )


def test_same_burst_rules():
    a = _a("a")
    assert same_burst(a, _a("b", dt=1.4), 2)
    assert same_burst(a, _a("b", dt=-2.0), 2)                  # inclusive
    assert same_burst(a, _a("b", w=2304, h=4608), 2)           # turned the phone mid-burst
    assert not same_burst(a, _a("b", dt=2.5), 2)
    assert not same_burst(a, _a("b", w=4000, h=3008), 2)       # a different camera / mode
    assert not same_burst(a, _a("b", kind=AssetKind.VIDEO), 2)
    assert not same_burst(a, _a("b", taken=False), 2)
    assert not same_burst(a, _a("a"), 2)                       # itself
    assert not same_burst(a, _a("b"), 0)                       # window off


def test_filter_skips_burst_mates_of_recent_shots(monkeypatch):
    import immframe.bursts as b
    clock = {"t": 1000.0}
    monkeypatch.setattr(b.time, "time", lambda: clock["t"])
    f = BurstFilter(2, 3600)
    f.note(_a("shown"))
    assert f.is_repeat(_a("mate", dt=1))
    assert not f.is_repeat(_a("other", dt=30))
    clock["t"] += 3601                                         # cooldown over
    assert not f.is_repeat(_a("mate", dt=1))


def test_thin_keeps_one_shot_per_burst_and_never_empties():
    f = BurstFilter(2, 3600)
    batch = [_a("a"), _a("b", dt=1), _a("c", dt=60), _a("d", dt=61)]
    assert [x.id for x in f.thin(batch)] == ["a", "c"]
    f.note(_a("a"))
    f.note(_a("c", dt=60))
    assert [x.id for x in f.thin([_a("b", dt=1), _a("d", dt=61)])] == ["b"]   # all repeats → keep one


def test_filter_off():
    f = BurstFilter(2, 0)
    f.note(_a("a"))
    assert not f.is_repeat(_a("b", dt=1))
    assert len(f.thin([_a("a"), _a("b", dt=1)])) == 2

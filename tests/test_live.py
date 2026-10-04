from __future__ import annotations

import pytest

from immframe.video.live import LiveSettings, MAX_REVERSE_S, recipe


def test_still_has_no_recipe():
    assert recipe(LiveSettings(mode="still")) is None


def test_once_plain_and_capped():
    r = recipe(LiveSettings(mode="once", play_s=0))
    assert r.vf == "" and r.loop == "no" and r.max_s is None and r.audio
    assert recipe(LiveSettings(mode="once", play_s=2.5)).max_s == 2.5


def test_loop_runs_for_play_s_or_repeats():
    r = recipe(LiveSettings(mode="loop", play_s=8))
    assert r.loop == "inf" and r.max_s == 8 and r.vf == ""
    r = recipe(LiveSettings(mode="loop", repeats=3))
    assert r.loop == "2" and r.max_s is None                    # loop-file=N → N extra plays
    assert recipe(LiveSettings(mode="loop", repeats=1)).loop == "no"


def test_loop_pause_freezes_last_frame():
    r = recipe(LiveSettings(mode="loop", pause_s=0.5))
    assert r.vf == "lavfi=[tpad=stop_mode=clone:stop_duration=0.5]"


def test_bounce_graph_trims_reverses_concats_and_mutes():
    r = recipe(LiveSettings(mode="bounce", play_s=10))
    assert r.vf.startswith(f"lavfi=[trim=end={MAX_REVERSE_S:g},setpts=PTS-STARTPTS,split[a][c];[c]reverse[r];")
    assert r.vf.endswith("[a][r]concat=n=2:v=1:a=0]")
    assert r.loop == "inf" and r.max_s == 10 and r.audio is False


def test_bounce_with_pause_holds_both_ends():
    r = recipe(LiveSettings(mode="bounce", pause_s=1))
    assert "[a]tpad=stop_mode=clone:stop_duration=1[f];" in r.vf
    assert "[r]tpad=stop_mode=clone:stop_duration=1[b];" in r.vf
    assert r.vf.endswith("[f][b]concat=n=2:v=1:a=0]")


def test_reverse():
    r = recipe(LiveSettings(mode="reverse"))
    assert r.vf == f"lavfi=[trim=end={MAX_REVERSE_S:g},setpts=PTS-STARTPTS,reverse]"
    assert r.loop == "no" and r.audio is False


def test_validation_clamps_and_rejects():
    v = LiveSettings(speed=99, pause_s=-1, repeats=-3, hold_s=500).validated()
    assert v.speed == 4.0 and v.pause_s == 0 and v.repeats == 0 and v.hold_s == 60
    assert LiveSettings(speed=0.01).validated().speed == 0.25
    with pytest.raises(ValueError):
        LiveSettings(mode="wobble").validated()
    with pytest.raises(ValueError):
        LiveSettings(after="later").validated()


def test_speed_passes_through():
    assert recipe(LiveSettings(mode="once", speed=0.5)).speed == 0.5


def test_order_validated():
    assert LiveSettings(order="video_first").validated().order == "video_first"
    with pytest.raises(ValueError, match="live_photo_order"):
        LiveSettings(order="sideways").validated()

"""VideoPlayer against a fake `mpv` module (no libmpv needed)."""
from __future__ import annotations

import sys
import types

import pytest


class _FakeMPV:
    def __init__(self, **opts):
        self.opts = opts
        self.props = {"fullscreen": opts.get("fullscreen") == "yes"}
        self.calls: list = []

    def __setitem__(self, key, value):
        self.props[key] = value
        self.calls.append(("set", key, value))

    def __getitem__(self, key):
        return self.props.get(key)

    def command(self, *args):
        self.calls.append(("command",) + args)

    def event_callback(self, _name):
        return lambda fn: fn

    def property_observer(self, _name):
        return lambda fn: fn

    @property
    def fullscreen(self):
        return self.props["fullscreen"]

    mpv_version = "fake"
    current_vo = "gpu"

    def terminate(self):
        pass


@pytest.fixture
def player(monkeypatch):
    fake = types.ModuleType("mpv")
    fake.MPV = _FakeMPV
    monkeypatch.setitem(sys.modules, "mpv", fake)
    from immframe.video.player import VideoPlayer
    return VideoPlayer(fullscreen=False)


def test_fullscreen_flag_reset_before_every_clip(player):
    """Regression: the reused MPV instance carried fullscreen=yes from the
    previous clip into the next window, which then collided with labwc's
    ToggleFullscreen rule and played in a tiny window."""
    m = player._mpv
    player.play("http://x/1", headers={"x-api-key": "k"})
    m.props["fullscreen"] = True                  # compositor / self-check made it fullscreen
    m.calls.clear()
    player.play("http://x/2", headers={"x-api-key": "k"})
    fs_set = m.calls.index(("set", "fullscreen", False))
    load = next(i for i, c in enumerate(m.calls) if c[:2] == ("command", "loadfile"))
    assert fs_set < load                          # reset happens before the new window maps


def test_fullscreen_true_config_is_respected(monkeypatch):
    fake = types.ModuleType("mpv")
    fake.MPV = _FakeMPV
    monkeypatch.setitem(sys.modules, "mpv", fake)
    from immframe.video.player import VideoPlayer
    p = VideoPlayer(fullscreen=True)              # plain desktop: MPV owns fullscreen
    p.play("http://x/1")
    assert ("set", "fullscreen", True) in p._mpv.calls

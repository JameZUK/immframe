"""Unit tests for the Controller class.

Only covers the parts that are reachable without a viewer / pi3d / mpv —
mostly the property setters and their interaction with shadow state.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from immframe.config import (
    Config,
    ControlConfig,
    ImmichConfig,
    SelectionConfig,
    VideoConfig,
    ViewerConfig,
)


def _config(**viewer_kw) -> Config:
    """Build a Config without going through YAML."""
    return Config(
        immich=ImmichConfig(url="http://example", api_key="k"),
        selection=SelectionConfig(),
        video=VideoConfig(),
        viewer=ViewerConfig(raw=viewer_kw),
        control=ControlConfig(),
    )


def _controller(**viewer_kw):
    """Instantiate Controller without starting it. ImmichClient is mocked
    out so no network is attempted."""
    cfg = _config(**viewer_kw)
    with patch("immframe.controller.ImmichClient") as ic, \
         patch("immframe.controller.PrefetchWorker") as pf:
        ic.return_value = MagicMock()
        pf.return_value = MagicMock()
        from immframe.controller import Controller
        return Controller(cfg)


def test_brightness_defaults_to_1():
    c = _controller()
    assert c.brightness == 1.0


def test_brightness_from_config():
    c = _controller(brightness=0.6)
    assert c.brightness == 0.6


def test_brightness_clamps():
    c = _controller()
    c.brightness = 5.0
    assert c.brightness == 1.0
    c.brightness = -0.5
    assert c.brightness == 0.0


def test_show_text_parses_space_separated_string():
    c = _controller(show_text="title date location")
    assert c.show_text == ["title", "date", "location"]


def test_show_text_parses_list():
    c = _controller(show_text=["title", "date"])
    assert c.show_text == ["title", "date"]


def test_show_text_filters_unknown():
    c = _controller(show_text="title nonexistent date")
    assert c.show_text == ["title", "date"]


def test_show_text_setter_accepts_list():
    c = _controller()
    c.show_text = ["caption", "location"]
    assert c.show_text == ["caption", "location"]


def test_show_text_setter_accepts_string():
    c = _controller()
    c.show_text = "title date"
    assert c.show_text == ["title", "date"]


def test_show_text_setter_empty():
    c = _controller()
    c.show_text = []
    assert c.show_text == []


def test_show_clock_default():
    c = _controller()
    assert c.show_clock is False


def test_show_clock_from_config():
    c = _controller(show_clock=True)
    assert c.show_clock is True


def test_show_clock_setter():
    c = _controller()
    c.show_clock = True
    assert c.show_clock is True


def test_time_delay_default():
    c = _controller()
    assert c.time_delay == 60.0


def test_time_delay_from_config():
    c = _controller(time_delay=30.0)
    assert c.time_delay == 30.0


def test_time_delay_clamps_low():
    c = _controller()
    c.time_delay = 0.1
    assert c.time_delay == 1.0


def test_fade_time_default():
    c = _controller()
    assert c.fade_time == 4.0


def test_fade_time_setter_clamps_negative():
    c = _controller()
    c.fade_time = -1.0
    assert c.fade_time == 0.0


def test_display_is_on_default():
    c = _controller()
    assert c.display_is_on is True


def test_setters_publish_state_via_mqtt():
    """Each setter calls self._publish_state which forwards to MQTT if wired."""
    c = _controller()
    mqtt = MagicMock()
    c._mqtt = mqtt
    c.brightness = 0.5
    c.show_clock = True
    c.show_text = ["title"]
    c.time_delay = 10
    c.fade_time = 1
    c.display_is_on = False
    # Each setter triggers exactly one publish
    assert mqtt.publish_state.call_count == 6


def test_setters_safe_without_viewer():
    """All setters should be no-op on viewer side but update shadow state."""
    c = _controller()
    # Viewer is None — these used to raise; now they should just shadow.
    c.brightness = 0.3
    c.display_is_on = False
    c.show_clock = True
    c.show_text = ["title"]
    assert c.brightness == 0.3
    assert c.display_is_on is False
    assert c.show_clock is True
    assert c.show_text == ["title"]


# ── Selection modes ────────────────────────────────────────────────────


def test_selection_mode_accepts_scene():
    c = _controller()
    c.selection_mode = "scene"
    assert c.selection_mode == "scene"


def test_selection_mode_rejects_unknown():
    c = _controller()
    with pytest.raises(ValueError):
        c.selection_mode = "everything"


def test_current_scene_none_outside_scene_mode():
    c = _controller()
    assert c.current_scene is None
    c.selection_mode = "album"
    assert c.current_scene is None


def test_current_scene_delegates_to_scene_selector():
    c = _controller()
    c.selection_mode = "scene"
    # The selector starts with no scene chosen yet
    assert c.current_scene is None
    # Simulate the selector picking one (without making a real network call)
    from immframe.immich.selector import SceneSelector
    assert isinstance(c._selector, SceneSelector)
    c._selector._current_scene = "beach"
    assert c.current_scene == "beach"

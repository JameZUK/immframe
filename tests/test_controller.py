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


def test_collage_label_uses_mode_and_count():
    c = _controller()
    assert c._collage_label(4) == "Random • 4 photos"
    assert c._collage_label(1) == "Random • 1 photo"


def test_collage_label_uses_scene_when_present():
    c = _controller()
    c.selection_mode = "scene"
    c._selector._current_scene = "beach"
    assert c._collage_label(3) == "beach • 3 photos"


def test_collage_enabled_toggle_pushes_config_to_prefetch():
    c = _controller()
    c.collage_enabled = True
    assert c.collage_enabled is True
    arg = c._prefetch.set_collage.call_args.args[0]
    assert arg is not None and arg.enabled is True


def test_collage_disabled_passes_disabled_config_to_prefetch():
    # The worker always gets the settings (so per-entry playlist collage still
    # works when the global switch is off); `enabled` carries the toggle state.
    c = _controller()
    c.collage_enabled = True
    c._prefetch.set_collage.reset_mock()
    c.collage_enabled = False
    arg = c._prefetch.set_collage.call_args.args[0]
    assert arg is not None and arg.enabled is False


def test_collage_layout_validates():
    c = _controller()
    with pytest.raises(ValueError):
        c.collage_layout = "spiral"
    c.collage_layout = "grid"
    assert c.collage_layout == "grid"


def test_playlist_build_carries_per_entry_collage_flag():
    from immframe.config import (
        Config, ImmichConfig, SelectionConfig, VideoConfig, ViewerConfig, ControlConfig,
    )
    from immframe.immich.selector import PlaylistSelector
    sel_cfg = SelectionConfig(
        default_mode="playlist",
        playlist=[
            {"mode": "random", "count": 10},
            {"mode": "random", "count": 3, "collage": True},
        ],
    )
    cfg = Config(
        immich=ImmichConfig(url="http://x", api_key="k"),
        selection=sel_cfg,
        video=VideoConfig(),
        viewer=ViewerConfig(raw={}),
        control=ControlConfig(),
    )
    with patch("immframe.controller.ImmichClient"), \
         patch("immframe.controller.PrefetchWorker"):
        from immframe.controller import Controller
        c = Controller(cfg)
    assert isinstance(c._selector, PlaylistSelector)
    flags = [entry[2] is not None for entry in c._selector._entries]
    assert flags == [False, True]
    # The collage entry carries a CollageConfig (global merged with overrides)
    assert c._selector._entries[1][2].enabled is True


def test_playlist_entry_collage_overrides():
    from immframe.config import (
        Config, ImmichConfig, SelectionConfig, VideoConfig, ViewerConfig, ControlConfig,
        CollageConfig,
    )
    sel_cfg = SelectionConfig(
        default_mode="playlist",
        playlist=[
            {"mode": "people", "count": 3, "collage": True,
             "tile_text": "people", "layout": "grid", "tiles": 4},
        ],
    )
    cfg = Config(
        immich=ImmichConfig(url="http://x", api_key="k"),
        selection=sel_cfg, video=VideoConfig(), viewer=ViewerConfig(raw={}),
        control=ControlConfig(),
        collage=CollageConfig(layout="auto", min_tiles=3, max_tiles=6, tile_text=""),
    )
    with patch("immframe.controller.ImmichClient"), \
         patch("immframe.controller.PrefetchWorker"):
        from immframe.controller import Controller
        c = Controller(cfg)
    cc = c._selector._entries[0][2]
    assert cc.tile_text == "people"
    assert cc.layout == "grid"
    assert cc.min_tiles == 4 and cc.max_tiles == 4


def test_collage_tiles_clamp_and_keep_min_le_max():
    c = _controller()
    c.collage_min_tiles = 100               # clamps to 12
    assert c.collage_min_tiles == 12
    assert c.collage_max_tiles == 12        # max raised to preserve min <= max
    c.collage_max_tiles = 3                 # below min → min lowered to match
    assert c.collage_max_tiles == 3
    assert c.collage_min_tiles == 3
    c.collage_min_tiles = 0                 # clamps up to 2
    assert c.collage_min_tiles == 2


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


# ── Selector construction for the new modes / entry options ─────────────


def _controller_with_selection(**sel_kw):
    from immframe.config import SelectionConfig
    cfg = _config()
    cfg.selection = SelectionConfig(**sel_kw)
    with patch("immframe.controller.ImmichClient") as ic, \
         patch("immframe.controller.PrefetchWorker") as pf:
        ic.return_value = MagicMock()
        pf.return_value = MagicMock()
        from immframe.controller import Controller
        return Controller(cfg)


def test_favorites_mode_builds_filtered_random():
    from immframe.immich.selector import RandomSelector
    c = _controller_with_selection(default_mode="favorites")
    sel = c._selector
    assert isinstance(sel, RandomSelector) and sel._favorites is True
    assert sel.current_scene == "Favourites"


def test_random_mode_carries_global_min_rating():
    c = _controller_with_selection(default_mode="random", min_rating=4)
    assert c._selector._min_rating == 4


def test_scene_source_forces_selector_mode():
    c = _controller_with_selection(default_mode="scene", scene_source="curated", smart_pages=6)
    assert c._selector._force_mode == "curated"
    assert c._selector._pages == 6
    c = _controller_with_selection(default_mode="scene", scene_source="auto")
    assert c._selector._force_mode is None


def test_people_mode_carries_threshold_and_favorites():
    c = _controller_with_selection(default_mode="people", people_min_photos=40, people_favorites_only=True)
    assert c._selector._min_photos == 40 and c._selector._favorites_only is True


def test_playlist_entry_overrides_for_new_options():
    from immframe.immich.selector import PeopleSelector, RandomSelector, SceneSelector
    c = _controller_with_selection(default_mode="playlist", playlist=[
        {"mode": "random", "count": 5, "favorites": True, "min_rating": 3, "tag_ids": ["t1"]},
        {"mode": "favorites", "count": 5},
        {"mode": "scene", "count": 5, "source": "curated", "pages": 2},
        {"mode": "people", "count": 5, "min_photos": 100, "favorites_only": True},
    ])
    entries = c._selector._entries
    r = entries[0][0]
    assert isinstance(r, RandomSelector) and r._favorites and r._min_rating == 3 and r._tag_ids == ["t1"]
    assert entries[1][0]._favorites is True
    sc = entries[2][0]
    assert isinstance(sc, SceneSelector) and sc._force_mode == "curated" and sc._pages == 2
    pp = entries[3][0]
    assert isinstance(pp, PeopleSelector) and pp._min_photos == 100 and pp._favorites_only


def test_playlist_entry_bad_scene_source_is_skipped():
    c = _controller_with_selection(default_mode="playlist", playlist=[
        {"mode": "scene", "count": 5, "source": "nonsense"},
        {"mode": "random", "count": 5},
    ])
    assert len(c._selector._entries) == 1


def test_selection_mode_setter_accepts_favorites():
    c = _controller()
    c.selection_mode = "favorites"
    assert c.selection_mode == "favorites"



# ── Curation: hide / favourite ───────────────────────────────────────────


def _asset(aid="abc12345-aaaa-bbbb-cccc-1234567890ab", *, favorite=False, live=None):
    from immframe.immich.models import Asset, AssetKind, GeoInfo
    return Asset(
        id=aid, kind=AssetKind.IMAGE, original_file_name="x.jpg", mime_type="image/jpeg",
        width=1, height=1, taken_at=None, geo=GeoInfo(None, None, None, None, None),
        camera_make=None, camera_model=None, title=None, caption=None, tag_names=(),
        people=(), favorite=favorite, live_photo_video_id=live,
    )


def _controller_with_hidden(tmp_path):
    from immframe.config import SelectionConfig
    cfg = _config()
    cfg.selection = SelectionConfig(hidden_file=str(tmp_path / "hidden.json"))
    with patch("immframe.controller.ImmichClient") as ic, \
         patch("immframe.controller.PrefetchWorker") as pf:
        ic.return_value = MagicMock()
        pf.return_value = MagicMock()
        from immframe.controller import Controller
        return Controller(cfg)


def test_hide_current_adds_to_list_archives_and_advances(tmp_path):
    c = _controller_with_hidden(tmp_path)
    c._current_asset = _asset(live="11111111-2222-3333-4444-555555555555")
    out = c.hide_current()
    assert out == {"hidden": c._current_asset.id, "archived": True, "error": None}
    assert c._current_asset.id in c._hidden
    assert "11111111-2222-3333-4444-555555555555" in c._hidden       # the motion clip too
    c._client.update_asset.assert_called_once_with(c._current_asset.id, visibility="archive")
    assert c._force_next_evt.is_set()
    assert c.hidden_count == 2


def test_hide_current_survives_immich_refusal(tmp_path):
    from immframe.immich.client import ImmichError
    c = _controller_with_hidden(tmp_path)
    c._current_asset = _asset()
    c._client.update_asset.side_effect = ImmichError("403 asset.update")
    out = c.hide_current()
    assert out["archived"] is False and "403" in out["error"]
    assert c._current_asset.id in c._hidden                          # hidden locally regardless
    assert c._force_next_evt.is_set()


def test_hide_current_rejects_no_asset_and_collage(tmp_path):
    c = _controller_with_hidden(tmp_path)
    with pytest.raises(ValueError):
        c.hide_current()
    c._current_asset = _asset("collage-7")
    with pytest.raises(ValueError):
        c.hide_current()
    c._client.update_asset.assert_not_called()


def test_favorite_current_toggles_and_updates_state(tmp_path):
    c = _controller_with_hidden(tmp_path)
    c._current_asset = _asset(favorite=False)
    out = c.favorite_current()
    assert out["favorite"] is True
    c._client.update_asset.assert_called_with(out["id"], favorite=True)
    assert c.current_asset.favorite is True                          # frozen asset replaced
    assert c.favorite_current()["favorite"] is False                 # toggles back
    assert c.favorite_current(True)["favorite"] is True              # explicit set


def test_favorite_current_propagates_immich_error(tmp_path):
    from immframe.immich.client import ImmichError
    c = _controller_with_hidden(tmp_path)
    c._current_asset = _asset()
    c._client.update_asset.side_effect = ImmichError("403")
    with pytest.raises(ImmichError):
        c.favorite_current()
    assert c.current_asset.favorite is False


def test_prefetch_gets_hidden_predicate(tmp_path):
    with patch("immframe.controller.ImmichClient") as ic, \
         patch("immframe.controller.PrefetchWorker") as pf:
        ic.return_value = MagicMock(); pf.return_value = MagicMock()
        from immframe.config import SelectionConfig
        from immframe.controller import Controller
        cfg = _config(); cfg.selection = SelectionConfig(hidden_file=str(tmp_path / "h.json"))
        c = Controller(cfg)
        pred = pf.call_args.kwargs["is_hidden"]
        assert pred("nope") is False
        c._hidden.add("nope")
        assert pred("nope") is True


# ── Portrait pairing ─────────────────────────────────────────────────────


def _item(aid, *, w=1000, h=1500, kind=None, live=None, path=True):
    from pathlib import Path
    from immframe.immich.models import Asset, AssetKind, GeoInfo
    a = Asset(
        id=aid, kind=kind or AssetKind.IMAGE, original_file_name=f"{aid}.jpg", mime_type="image/jpeg",
        width=w, height=h, taken_at=None, geo=GeoInfo(None, None, None, None, None),
        camera_make=None, camera_model=None, title=None, caption=None, tag_names=(),
        people=(), favorite=False, live_photo_video_id=live,
    )
    return (Path(f"/nonexistent/{aid}.jpg") if path else None, a, None)


def test_pairable_rules():
    from immframe.controller import Controller
    from immframe.immich.models import AssetKind
    assert Controller._pairable(_item("p"))
    assert not Controller._pairable(_item("land", w=1500, h=1000))
    assert not Controller._pairable(_item("vid", kind=AssetKind.VIDEO))
    assert not Controller._pairable(_item("live", live="clip"))
    assert not Controller._pairable(_item("collage-3"))
    assert not Controller._pairable(_item("nopath", path=False))


def test_pair_for_pairs_two_portraits():
    c = _controller()
    second = _item("p2")
    c._prefetch.next.return_value = second
    assert c._pair_for(_item("p1")) is second
    assert c._pending_item is None


def test_pair_for_holds_back_non_portrait_as_next_slide():
    c = _controller()
    land = _item("land", w=1500, h=1000)
    c._prefetch.next.return_value = land
    assert c._pair_for(_item("p1")) is None
    assert c._pending_item is land
    # The held-back item is what comes off next — without touching the queue.
    c._prefetch.next.reset_mock()
    assert c._take_item(timeout=1.0) is land
    c._prefetch.next.assert_not_called()


def test_pair_for_skips_when_first_is_not_portrait_or_queue_empty():
    c = _controller()
    c._prefetch.next.return_value = _item("p2")
    assert c._pair_for(_item("land", w=1500, h=1000)) is None
    c._prefetch.next.assert_not_called()
    c._prefetch.next.return_value = None
    assert c._pair_for(_item("p1")) is None


def test_pending_item_dropped_after_selection_change(tmp_path):
    c = _controller()
    held = _item("old")
    held[0].parent  # path object only; nothing on disk to unlink
    c._pending_item = held
    c.selection_mode = "random"                   # marks pending stale
    fresh = _item("fresh")
    c._prefetch.next.return_value = fresh
    assert c._take_item(timeout=1.0) is fresh


def test_portrait_pairs_config_toggle():
    assert _controller()._portrait_pairs is True
    assert _controller(portrait_pairs=False)._portrait_pairs is False

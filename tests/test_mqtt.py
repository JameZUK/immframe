from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from immframe.config import MqttConfig
from immframe.immich.models import Asset, AssetKind, GeoInfo
from immframe.interfaces.mqtt import (
    DISCOVERY_PREFIX,
    ENTITIES,
    MqttInterface,
    _apply_cmd,
    _attrs_of,
    _state_of,
)


# ── Controller stub ─────────────────────────────────────────────────────────

class _StubController:
    """Minimal duck-typed controller for MQTT tests."""

    def __init__(self):
        self.paused = False
        self.selection_mode = "random"
        self.album_ids = ["a", "b"]
        self.smart_query = "sunsets"
        self.people_ids = []
        self.brightness = 1.0
        self.display_is_on = True
        self.show_text = ["title", "date"]
        self.show_clock = False
        self.time_delay = 60.0
        self.fade_time = 4.0
        self.collage_enabled = False
        self.collage_layout = "auto"
        self.collage_min_tiles = 3
        self.collage_max_tiles = 6
        self.current_asset: Asset | None = None
        self.current_scene: str | None = None
        self.next_calls = 0

    def next(self):
        self.next_calls += 1


def _asset(aid: str = "xyz") -> Asset:
    return Asset(
        id=aid,
        kind=AssetKind.IMAGE,
        original_file_name=f"{aid}.jpg",
        mime_type="image/jpeg",
        width=4000,
        height=3000,
        taken_at=None,
        geo=GeoInfo(None, None, "Reykjavík", None, "Iceland"),
        camera_make="Canon",
        camera_model="EOS R6",
        title=None,
        caption=None,
        tag_names=(),
        people=(),
        favorite=False,
        live_photo_video_id=None,
    )


# ── Pure-function tests (no paho) ───────────────────────────────────────────


def test_entities_have_unique_object_ids():
    seen = set()
    for e in ENTITIES:
        assert e.object_id not in seen, f"duplicate object_id: {e.object_id}"
        seen.add(e.object_id)


def test_state_of_paused():
    c = _StubController()
    paused = next(e for e in ENTITIES if e.object_id == "paused")
    assert _state_of(c, paused) == "OFF"
    c.paused = True
    assert _state_of(c, paused) == "ON"


def test_state_of_album_ids_csv():
    c = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "album_ids")
    assert _state_of(c, e) == "a, b"


def test_state_of_current_asset_empty():
    c = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "current_asset")
    assert _state_of(c, e) == ""


def test_attrs_of_current_asset():
    c = _StubController()
    c.current_asset = _asset("xyz")
    e = next(e for e in ENTITIES if e.object_id == "current_asset")
    attrs = _attrs_of(c, e)
    assert attrs is not None
    assert attrs["file"] == "xyz.jpg"
    assert attrs["city"] == "Reykjavík"
    assert attrs["country"] == "Iceland"
    assert attrs["camera"] == "Canon EOS R6"
    assert attrs["kind"] == "IMAGE"
    assert attrs["scene"] is None     # no scene in default state


def test_attrs_includes_current_scene():
    c = _StubController()
    c.current_asset = _asset("xyz")
    c.current_scene = "beach"
    e = next(e for e in ENTITIES if e.object_id == "current_asset")
    attrs = _attrs_of(c, e)
    assert attrs["scene"] == "beach"


def test_apply_cmd_selection_mode_scene():
    from immframe.interfaces.mqtt import _apply_cmd
    c = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "selection_mode")
    _apply_cmd(c, e, "scene")
    assert c.selection_mode == "scene"


def test_apply_cmd_paused():
    c = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "paused")
    _apply_cmd(c, e, "ON")
    assert c.paused is True
    _apply_cmd(c, e, "OFF")
    assert c.paused is False


def test_apply_cmd_selection_mode():
    c = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "selection_mode")
    _apply_cmd(c, e, "album")
    assert c.selection_mode == "album"


def test_apply_cmd_album_ids_parses_csv():
    c = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "album_ids")
    _apply_cmd(c, e, "one,  two ,three")
    assert c.album_ids == ["one", "two", "three"]


def test_apply_cmd_album_ids_empty():
    c = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "album_ids")
    _apply_cmd(c, e, "")
    assert c.album_ids == []


def test_apply_cmd_next_button():
    c = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "next")
    _apply_cmd(c, e, "PRESS")
    assert c.next_calls == 1


# ── Collage ──────────────────────────────────────────────────────────────────


def test_collage_entities_present():
    ids = {e.object_id for e in ENTITIES}
    assert {"collage_enabled", "collage_layout", "collage_min_tiles",
            "collage_max_tiles"} <= ids


def test_state_of_collage():
    c = _StubController()
    c.collage_enabled = True
    c.collage_layout = "grid"
    c.collage_min_tiles = 4
    c.collage_max_tiles = 8
    by_id = {e.object_id: e for e in ENTITIES}
    assert _state_of(c, by_id["collage_enabled"]) == "ON"
    assert _state_of(c, by_id["collage_layout"]) == "grid"
    assert _state_of(c, by_id["collage_min_tiles"]) == "4"
    assert _state_of(c, by_id["collage_max_tiles"]) == "8"


def test_apply_cmd_collage_enabled():
    c = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "collage_enabled")
    _apply_cmd(c, e, "ON")
    assert c.collage_enabled is True
    _apply_cmd(c, e, "OFF")
    assert c.collage_enabled is False


def test_apply_cmd_collage_layout():
    c = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "collage_layout")
    _apply_cmd(c, e, "golden_ratio")
    assert c.collage_layout == "golden_ratio"


def test_apply_cmd_collage_tiles_accepts_ha_float_payload():
    """HA number entities send payloads like '5' or '5.0'."""
    c = _StubController()
    emin = next(e for e in ENTITIES if e.object_id == "collage_min_tiles")
    emax = next(e for e in ENTITIES if e.object_id == "collage_max_tiles")
    _apply_cmd(c, emin, "5.0")
    _apply_cmd(c, emax, "9")
    assert c.collage_min_tiles == 5
    assert c.collage_max_tiles == 9


# ── MqttInterface tests with mocked paho ────────────────────────────────────


def _cfg(**kw) -> MqttConfig:
    base = {"enabled": True, "host": "broker.test", "port": 1883, "user": "", "password": "", "base_topic": "immframe"}
    base.update(kw)
    return MqttConfig(**base)


@pytest.fixture
def mqtt_mod():
    with patch("immframe.interfaces.mqtt.mqtt") as m:
        # paho.mqtt.client.CallbackAPIVersion is referenced; provide a dummy
        m.CallbackAPIVersion.VERSION2 = "v2"
        client = MagicMock()
        m.Client.return_value = client
        yield m


def test_start_sets_lwt_and_connects(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    client = mqtt_mod.Client.return_value
    client.will_set.assert_called_once_with("immframe/availability", "offline", retain=True)
    client.connect_async.assert_called_once_with("broker.test", 1883, keepalive=60)
    client.loop_start.assert_called_once()


def test_start_sets_username_when_provided(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(user="u", password="p"), ctrl)
    iface.start()
    client = mqtt_mod.Client.return_value
    client.username_pw_set.assert_called_once_with("u", "p")


def test_start_omits_username_when_blank(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    mqtt_mod.Client.return_value.username_pw_set.assert_not_called()


def _fire_on_connect(iface: MqttInterface, mqtt_mod):
    """Simulate paho calling on_connect with success."""
    client = mqtt_mod.Client.return_value
    reason_code = SimpleNamespace(is_failure=False)
    iface._on_connect(client, None, {}, reason_code)


def test_on_connect_publishes_availability(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    _fire_on_connect(iface, mqtt_mod)
    client = mqtt_mod.Client.return_value
    avail_calls = [c for c in client.publish.call_args_list if c.args[0] == "immframe/availability"]
    assert any(c.args[1] == "online" for c in avail_calls)


def test_on_connect_subscribes_command_topics(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    _fire_on_connect(iface, mqtt_mod)
    client = mqtt_mod.Client.return_value
    subscribed = {c.args[0] for c in client.subscribe.call_args_list}
    # Should have subscribed to every commandable entity's /set topic
    expected = {f"immframe/{e.object_id}/set" for e in ENTITIES if e.has_command}
    assert subscribed == expected


def test_on_connect_publishes_discovery(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    _fire_on_connect(iface, mqtt_mod)
    client = mqtt_mod.Client.return_value
    discovery_calls = [c for c in client.publish.call_args_list if c.args[0].startswith(f"{DISCOVERY_PREFIX}/")]
    # One discovery message per entity
    assert len(discovery_calls) == len(ENTITIES)
    # Spot-check the paused switch
    paused_call = next(c for c in discovery_calls if c.args[0].endswith("/paused/config"))
    payload = json.loads(paused_call.args[1])
    assert payload["name"] == "Paused"
    assert payload["command_topic"] == "immframe/paused/set"
    assert payload["state_topic"] == "immframe/paused/state"
    assert payload["payload_on"] == "ON"
    assert payload["payload_off"] == "OFF"
    assert payload["device"]["identifiers"] == ["immframe"]


def test_on_connect_publishes_number_min_max(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    _fire_on_connect(iface, mqtt_mod)
    client = mqtt_mod.Client.return_value
    brightness_call = next(
        c for c in client.publish.call_args_list
        if c.args[0].endswith("/brightness/config")
    )
    payload = json.loads(brightness_call.args[1])
    assert payload["min"] == 0.0
    assert payload["max"] == 1.0
    assert payload["step"] == 0.05
    time_delay_call = next(
        c for c in client.publish.call_args_list
        if c.args[0].endswith("/time_delay/config")
    )
    td = json.loads(time_delay_call.args[1])
    assert td["min"] == 1.0
    assert td["unit_of_measurement"] == "s"


def test_apply_cmd_brightness(mqtt_mod):
    from immframe.interfaces.mqtt import _apply_cmd
    ctrl = _StubController()
    brightness_e = next(e for e in ENTITIES if e.object_id == "brightness")
    _apply_cmd(ctrl, brightness_e, "0.5")
    assert ctrl.brightness == 0.5


def test_apply_cmd_show_text(mqtt_mod):
    from immframe.interfaces.mqtt import _apply_cmd
    ctrl = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "show_text")
    _apply_cmd(ctrl, e, "title, date, location")
    assert ctrl.show_text == ["title", "date", "location"]


def test_apply_cmd_time_delay(mqtt_mod):
    from immframe.interfaces.mqtt import _apply_cmd
    ctrl = _StubController()
    e = next(e for e in ENTITIES if e.object_id == "time_delay")
    _apply_cmd(ctrl, e, "30")
    assert ctrl.time_delay == 30.0


def test_on_connect_publishes_select_options(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    _fire_on_connect(iface, mqtt_mod)
    client = mqtt_mod.Client.return_value
    select_call = next(
        c for c in client.publish.call_args_list
        if c.args[0].startswith(f"{DISCOVERY_PREFIX}/select/")
    )
    payload = json.loads(select_call.args[1])
    assert payload["options"] == [
        "random", "album", "smart", "scene", "people",
        "memory", "recent", "playlist",
    ]


def test_on_connect_publishes_state(mqtt_mod):
    ctrl = _StubController()
    ctrl.paused = True
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    _fire_on_connect(iface, mqtt_mod)
    client = mqtt_mod.Client.return_value
    paused_state = next(
        c for c in client.publish.call_args_list
        if c.args[0] == "immframe/paused/state"
    )
    assert paused_state.args[1] == "ON"


def test_on_message_dispatches_paused(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    _fire_on_connect(iface, mqtt_mod)

    msg = SimpleNamespace(topic="immframe/paused/set", payload=b"ON")
    iface._on_message(mqtt_mod.Client.return_value, None, msg)
    assert ctrl.paused is True


def test_on_message_dispatches_select(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    _fire_on_connect(iface, mqtt_mod)

    msg = SimpleNamespace(topic="immframe/selection_mode/set", payload=b"smart")
    iface._on_message(mqtt_mod.Client.return_value, None, msg)
    assert ctrl.selection_mode == "smart"


def test_on_message_dispatches_button(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    _fire_on_connect(iface, mqtt_mod)

    msg = SimpleNamespace(topic="immframe/next/set", payload=b"PRESS")
    iface._on_message(mqtt_mod.Client.return_value, None, msg)
    assert ctrl.next_calls == 1


def test_on_message_unknown_topic_ignored(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    _fire_on_connect(iface, mqtt_mod)

    before = (ctrl.paused, ctrl.selection_mode, ctrl.next_calls)
    msg = SimpleNamespace(topic="random/garbage/topic", payload=b"x")
    iface._on_message(mqtt_mod.Client.return_value, None, msg)
    after = (ctrl.paused, ctrl.selection_mode, ctrl.next_calls)
    assert before == after


def test_publish_state_noop_when_not_connected(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    # connect callback NOT fired → _connected is unset
    client = mqtt_mod.Client.return_value
    client.publish.reset_mock()
    iface.publish_state()
    # publishes are skipped — connect-side ones were before reset; should remain at 0 after
    assert client.publish.call_count == 0


def test_stop_publishes_offline_and_disconnects(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    iface.stop()
    client = mqtt_mod.Client.return_value
    # Final offline publish
    last_avail = [c for c in client.publish.call_args_list if c.args[0] == "immframe/availability"]
    assert last_avail and last_avail[-1].args[1] == "offline"
    client.loop_stop.assert_called_once()
    client.disconnect.assert_called_once()


def test_stop_is_idempotent(mqtt_mod):
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    iface.stop()
    iface.stop()


def test_on_message_failure_doesnt_propagate(mqtt_mod):
    """A bad command payload should be logged but not raise out of paho's thread."""
    ctrl = _StubController()
    iface = MqttInterface(_cfg(), ctrl)
    iface.start()
    _fire_on_connect(iface, mqtt_mod)

    # selection_mode validation rejects unknown values via ValueError; the
    # interface should swallow it.
    class _RaiseOnSet:
        @property
        def selection_mode(self):
            return "random"
        @selection_mode.setter
        def selection_mode(self, v):
            raise ValueError("nope")

    # patch the controller property to raise
    type(ctrl).selection_mode = property(
        lambda self: "random",
        lambda self, v: (_ for _ in ()).throw(ValueError("nope")),
    )
    try:
        msg = SimpleNamespace(topic="immframe/selection_mode/set", payload=b"bogus")
        iface._on_message(mqtt_mod.Client.return_value, None, msg)
    finally:
        # restore so other tests aren't affected
        del type(ctrl).selection_mode

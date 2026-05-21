"""MQTT control plane with Home Assistant discovery.

Subscribes to command topics for state-mutating actions (paused,
selection_mode, album_ids, smart_query, next button). Publishes state
changes so HA reflects them. Publishes HA discovery payloads on connect
with retain=True so entities reappear after broker restarts.

Topic structure:
    <base>/availability               -> "online" / "offline" (LWT)
    <base>/<entity>/state             -> current value
    <base>/<entity>/set               <- new value (commandable entities)
    <base>/<entity>/attributes        -> JSON object for sensor attributes

This module is data-driven: every entity is one row in `ENTITIES` and one
case in `_state_of`/`_attrs_of`/`_apply_cmd`. Adding an entity is editing
those four lists, not 80 lines of boilerplate.

Threading: paho runs its own network thread. `_on_*` callbacks fire there;
they call controller setters which already need to be thread-safe (the
controller's own contract).
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt

from ..config import MqttConfig

if TYPE_CHECKING:
    from ..controller import Controller

log = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"
DEVICE_MANUFACTURER = "immframe"
DEVICE_MODEL = "Immich slideshow"


@dataclass(frozen=True)
class Entity:
    component: str                           # 'switch', 'select', 'text', 'button', 'sensor'
    object_id: str
    name: str
    icon: str | None = None
    options: tuple[str, ...] | None = None   # for 'select'
    has_state: bool = True                   # False for 'button'
    has_command: bool = True                 # False for 'sensor'
    payload_on: str = "ON"
    payload_off: str = "OFF"
    payload_press: str = "PRESS"


ENTITIES: tuple[Entity, ...] = (
    Entity("switch", "paused", "Paused", icon="mdi:pause"),
    Entity(
        "select", "selection_mode", "Selection mode",
        options=("random", "album", "smart"), icon="mdi:image-multiple",
    ),
    Entity("text", "album_ids", "Album IDs", icon="mdi:image-album"),
    Entity("text", "smart_query", "Smart query", icon="mdi:magnify"),
    Entity(
        "button", "next", "Next", icon="mdi:skip-next",
        has_state=False,
    ),
    Entity(
        "sensor", "current_asset", "Current asset", icon="mdi:image",
        has_command=False,
    ),
)


def _state_of(controller: "Controller", entity: Entity) -> str:
    """Read the current state for an entity from the controller."""
    oid = entity.object_id
    if oid == "paused":
        return "ON" if controller.paused else "OFF"
    if oid == "selection_mode":
        return controller.selection_mode
    if oid == "album_ids":
        return ", ".join(controller.album_ids)
    if oid == "smart_query":
        return controller.smart_query
    if oid == "current_asset":
        a = controller.current_asset
        return a.id if a is not None else ""
    return ""


def _attrs_of(controller: "Controller", entity: Entity) -> dict | None:
    """Optional JSON attributes for sensor-type entities. None = no attrs."""
    if entity.object_id == "current_asset":
        a = controller.current_asset
        if a is None:
            return {}
        camera = " ".join(p for p in (a.camera_make, a.camera_model) if p)
        return {
            "file": a.original_file_name,
            "taken_at": a.taken_at.isoformat() if a.taken_at is not None else None,
            "city": a.geo.city,
            "country": a.geo.country,
            "camera": camera or None,
            "kind": a.kind.value,
            "favorite": a.favorite,
        }
    return None


def _apply_cmd(controller: "Controller", entity: Entity, payload: str) -> None:
    """Apply an incoming MQTT command to the controller."""
    oid = entity.object_id
    if oid == "paused":
        controller.paused = (payload.strip().upper() == entity.payload_on)
    elif oid == "selection_mode":
        controller.selection_mode = payload.strip()
    elif oid == "album_ids":
        controller.album_ids = [s.strip() for s in payload.split(",") if s.strip()]
    elif oid == "smart_query":
        controller.smart_query = payload.strip()
    elif oid == "next":
        controller.next()
    else:
        log.debug("no command handler for %s", oid)


def _topic(base: str, entity: Entity, suffix: str) -> str:
    return f"{base}/{entity.object_id}/{suffix}"


class MqttInterface:
    def __init__(self, config: MqttConfig, controller: "Controller") -> None:
        self._cfg = config
        self._ctrl = controller
        self._base = config.base_topic
        self._device_uid = config.base_topic            # one device per immframe instance
        self._client: mqtt.Client | None = None
        self._connected = threading.Event()
        self._lock = threading.Lock()

    # ── Lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._device_uid,
        )
        if self._cfg.user:
            client.username_pw_set(self._cfg.user, self._cfg.password or None)
        client.will_set(self._avail_topic(), "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        try:
            client.connect_async(self._cfg.host, self._cfg.port, keepalive=60)
            client.loop_start()
        except Exception as e:
            log.warning("mqtt connect_async failed: %s", e)
            return
        self._client = client

    def stop(self) -> None:
        c = self._client
        if c is None:
            return
        try:
            c.publish(self._avail_topic(), "offline", retain=True)
            c.loop_stop()
            c.disconnect()
        except Exception as e:
            log.debug("mqtt stop: %s", e)
        self._client = None
        self._connected.clear()

    # ── Public hook ─────────────────────────────────────────────────────
    def publish_state(self) -> None:
        """Publish state (and attributes) for every entity to its topic.

        Safe to call from any thread. No-op until the broker connection is
        established.
        """
        c = self._client
        if c is None or not self._connected.is_set():
            return
        with self._lock:
            for entity in ENTITIES:
                if entity.has_state:
                    state = _state_of(self._ctrl, entity)
                    c.publish(_topic(self._base, entity, "state"), state, retain=True)
                attrs = _attrs_of(self._ctrl, entity)
                if attrs is not None:
                    c.publish(
                        _topic(self._base, entity, "attributes"),
                        json.dumps(attrs),
                        retain=True,
                    )

    # ── Topics ──────────────────────────────────────────────────────────
    def _avail_topic(self) -> str:
        return f"{self._base}/availability"

    # ── paho callbacks ──────────────────────────────────────────────────
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        # paho v2 reason_code is a ReasonCode object or int 0 on success
        failed = getattr(reason_code, "is_failure", None)
        if failed is None:
            failed = reason_code != 0
        if failed:
            log.warning("mqtt connect failed: %s", reason_code)
            return
        log.info("mqtt connected to %s:%d", self._cfg.host, self._cfg.port)
        self._connected.set()
        client.publish(self._avail_topic(), "online", retain=True)
        for entity in ENTITIES:
            if entity.has_command:
                client.subscribe(_topic(self._base, entity, "set"))
        self._publish_discovery()
        self.publish_state()

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties=None
    ) -> None:
        self._connected.clear()
        log.info("mqtt disconnected: %s", reason_code)
        # paho's loop_start handles automatic reconnect; on_connect will
        # re-republish discovery + state when it fires again.

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = message.payload.decode("utf-8", errors="replace")
        except Exception:
            log.debug("non-text mqtt payload on %s", message.topic)
            return
        for entity in ENTITIES:
            if not entity.has_command:
                continue
            if message.topic == _topic(self._base, entity, "set"):
                log.info("mqtt cmd %s=%r", entity.object_id, payload[:80])
                try:
                    _apply_cmd(self._ctrl, entity, payload)
                except Exception as e:
                    log.warning("apply_cmd(%s) failed: %s", entity.object_id, e)
                # Echo state back so HA's UI stays in sync.
                self.publish_state()
                return
        log.debug("unknown mqtt topic: %s", message.topic)

    # ── Discovery ───────────────────────────────────────────────────────
    def _publish_discovery(self) -> None:
        c = self._client
        if c is None:
            return
        device = {
            "identifiers": [self._device_uid],
            "name": self._device_uid,
            "manufacturer": DEVICE_MANUFACTURER,
            "model": DEVICE_MODEL,
        }
        for entity in ENTITIES:
            payload: dict[str, object] = {
                "name": entity.name,
                "unique_id": f"{self._device_uid}_{entity.object_id}",
                "availability_topic": self._avail_topic(),
                "device": device,
            }
            if entity.icon:
                payload["icon"] = entity.icon
            if entity.has_state:
                payload["state_topic"] = _topic(self._base, entity, "state")
            if entity.has_command:
                payload["command_topic"] = _topic(self._base, entity, "set")
            if entity.component == "switch":
                payload["payload_on"] = entity.payload_on
                payload["payload_off"] = entity.payload_off
            elif entity.component == "select" and entity.options:
                payload["options"] = list(entity.options)
            elif entity.component == "button":
                payload["payload_press"] = entity.payload_press
            elif entity.component == "sensor":
                payload["json_attributes_topic"] = _topic(self._base, entity, "attributes")

            discovery_topic = (
                f"{DISCOVERY_PREFIX}/{entity.component}/{self._device_uid}/"
                f"{entity.object_id}/config"
            )
            c.publish(discovery_topic, json.dumps(payload), retain=True)

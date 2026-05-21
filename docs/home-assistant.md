# Home Assistant integration

immframe publishes MQTT discovery messages when started with
`control.mqtt.enabled: true`, so Home Assistant picks the device up
automatically. You don't need to write any YAML to *see* the entities — they
appear under **Settings → Devices & Services → MQTT → immframe**.

This page covers what those entities are, how to wire the current image into
HA, and a complete Lovelace card example.

## Auto-discovered entities

| Entity | Type | Purpose |
|---|---|---|
| `switch.immframe_paused` | switch | Pause / resume the slideshow |
| `select.immframe_selection_mode` | select | random / album / smart |
| `text.immframe_album_ids` | text | Comma-separated album UUIDs |
| `text.immframe_smart_query` | text | CLIP search query |
| `button.immframe_next` | button | Force-advance to the next slide |
| `switch.immframe_display_is_on` | switch | Turn the HDMI output on/off |
| `number.immframe_brightness` | number (0.0–1.0) | Render brightness |
| `number.immframe_time_delay` | number (1–3600 s) | Slide duration |
| `number.immframe_fade_time` | number (0–30 s) | Crossfade duration |
| `text.immframe_show_text` | text | Overlay fields (comma-separated, subset of `title,caption,name,date,location,folder`) |
| `switch.immframe_show_clock` | switch | Show / hide the clock overlay |
| `sensor.immframe_current_asset` | sensor | Current asset UUID; attributes carry file, taken_at, city, country, camera, kind, favorite |

A device with these entities is created with the identifier set to your
`control.mqtt.base_topic` (default `immframe`).

## Showing the current image

The MQTT layer only exposes the asset *ID*. The HTTP control plane has the
proxy endpoint:

```
GET http://<pi-ip>:8080/api/image/<asset_id>
```

That endpoint serves the preview JPEG straight from Immich (auth on by
default). Combine the two with a [generic camera](https://www.home-assistant.io/integrations/generic/):

```yaml
# configuration.yaml
camera:
  - platform: generic
    name: immframe_current
    still_image_url: >-
      http://192.168.1.42:8080/api/image/{{
        states('sensor.immframe_current_asset')
      }}
    username: !secret immframe_http_user
    password: !secret immframe_http_password
    authentication: basic
    framerate: 0.2          # poll the URL only when HA wants a refresh
    verify_ssl: false       # not using HTTPS in immframe's HTTP control plane
```

The template re-evaluates every time `sensor.immframe_current_asset` changes
(which immframe publishes on every slide transition), so HA fetches the new
image on the next refresh.

## Sample Lovelace card

Drop this in a dashboard. Adjust entity IDs if you set a non-default
`base_topic`.

```yaml
type: vertical-stack
title: immframe
cards:
  - type: picture-entity
    entity: camera.immframe_current
    show_state: false
    show_name: false
    tap_action: { action: more-info }

  - type: entities
    title: Now showing
    entities:
      - entity: sensor.immframe_current_asset
        name: Asset
        secondary_info: last-changed
      - type: attribute
        entity: sensor.immframe_current_asset
        attribute: file
        name: File
      - type: attribute
        entity: sensor.immframe_current_asset
        attribute: taken_at
        name: Taken
      - type: attribute
        entity: sensor.immframe_current_asset
        attribute: city
        name: City
      - type: attribute
        entity: sensor.immframe_current_asset
        attribute: country
        name: Country
      - type: attribute
        entity: sensor.immframe_current_asset
        attribute: camera
        name: Camera

  - type: horizontal-stack
    cards:
      - type: button
        entity: switch.immframe_paused
        name: Pause
        icon: mdi:pause
        tap_action: { action: toggle }
      - type: button
        entity: button.immframe_next
        name: Next
        icon: mdi:skip-next
        tap_action: { action: press }

  - type: entities
    title: Selection
    entities:
      - entity: select.immframe_selection_mode
        name: Mode
      - entity: text.immframe_album_ids
        name: Albums
      - entity: text.immframe_smart_query
        name: Smart query

  - type: entities
    title: Slideshow
    entities:
      - entity: number.immframe_time_delay
        name: Slide duration
      - entity: number.immframe_fade_time
        name: Fade duration
      - entity: number.immframe_brightness
        name: Brightness

  - type: entities
    title: Display
    entities:
      - entity: switch.immframe_display_is_on
        name: Display on
      - entity: switch.immframe_show_clock
        name: Clock
      - entity: text.immframe_show_text
        name: Overlay fields
```

## Automation snippets

**Dim the frame at night and brighten in the morning:**

```yaml
automation:
  - alias: immframe dim at night
    trigger:
      platform: time
      at: "22:00:00"
    action:
      service: number.set_value
      target:
        entity_id: number.immframe_brightness
      data:
        value: 0.3

  - alias: immframe brighten in the morning
    trigger:
      platform: time
      at: "07:00:00"
    action:
      service: number.set_value
      target:
        entity_id: number.immframe_brightness
      data:
        value: 1.0
```

**Pause when no-one's home:**

```yaml
automation:
  - alias: immframe pause when away
    trigger:
      platform: state
      entity_id: group.family
      to: not_home
    action:
      service: switch.turn_on
      target:
        entity_id: switch.immframe_paused

  - alias: immframe resume when home
    trigger:
      platform: state
      entity_id: group.family
      to: home
    action:
      service: switch.turn_off
      target:
        entity_id: switch.immframe_paused
```

**Show holiday photos for a week before a trip:**

```yaml
automation:
  - alias: immframe show holiday album
    trigger:
      platform: time
      at: "08:00:00"
    condition:
      - condition: template
        value_template: >-
          {{ (as_timestamp(states('input_datetime.next_trip'))
              - as_timestamp(now())) < 7 * 86400 }}
    action:
      - service: select.select_option
        target:
          entity_id: select.immframe_selection_mode
        data:
          option: album
      - service: text.set_value
        target:
          entity_id: text.immframe_album_ids
        data:
          value: "<your-holiday-album-uuid>"
```

## Troubleshooting

| Problem | Likely cause |
|---|---|
| Device doesn't appear in HA | Check `control.mqtt.enabled: true` in immframe config, MQTT broker connectivity, and that HA's MQTT integration uses the same broker |
| Entities appear but stay "unavailable" | LWT — the immframe process isn't running, or the connection dropped. Restart and check `journalctl --user -u immframe -f` |
| Image stays blank | Verify the generic-camera URL hits the immframe HTTP server (check `control.http.enabled: true`), and that the auth credentials match |
| `text.immframe_*` entities reject your input | HA imposes character limits on text entities. The CSV form of `show_text` is intentionally short |

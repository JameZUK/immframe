# immframe

A picture-frame slideshow that streams photos and videos directly from an
[Immich](https://immich.app) server. Designed for a Raspberry Pi wired to a
TV / dedicated display, with Home Assistant integration via MQTT and a small
REST API.

Derived from [picframe](https://github.com/helgeerbe/picframe) — the
pi3d-based renderer, mat compositor and overlay text code are vendored from
picframe; the filesystem scanner, SQLite cache, EXIF/IPTC parser and
reverse-geocoder are dropped in favour of Immich's API.

## Why

If you already run Immich, you have a single source of truth for your photos:
metadata, geocoding, smart search, faces, albums, tags. There's no reason to
also maintain a separate filesystem cache + EXIF parser on a Raspberry Pi
just to show those photos on a frame. immframe defers everything it can to
Immich:

| Deferred to Immich | Dropped from picframe |
|---|---|
| EXIF / IPTC parsing | `get_image_meta.py` |
| Reverse geocoding | `geo_reverse.py` |
| Image rotation (preview JPEGs are pre-rotated) | `__orientate_image` |
| Storage / dedup / search | `image_cache.py` (SQLite) |

What's left for us: a render loop, a small prefetch queue, and the slideshow
UX picframe got right.

## Features

- Four selection modes, switchable at runtime:
  - **Random** — `POST /api/search/random` across the whole library
  - **Album** — random shuffle within one or more albums
  - **Smart** — CLIP search (e.g. *"family at the beach"*)
  - **Scene** — Immich's auto-discovered CLIP scene labels; picks a random
    scene (*beach*, *mountain*, *wedding*, …), shows ~25 photos from it,
    then rotates to a new scene. Zero config.
- Crossfades, blur edges, Ken Burns, optional mat compositing (from
  picframe's renderer, unchanged)
- Date / location overlay text — fields come straight from Immich, no
  EXIF parsing
- Direct video streaming via [python-mpv](https://github.com/jaseg/python-mpv)
  with KMS/DRM output on the Pi — no local download, no transcode
- Graceful degradation if Immich is briefly unavailable
- Home Assistant integration via MQTT auto-discovery — see
  [docs/home-assistant.md](./docs/home-assistant.md) for entities and a
  ready-made Lovelace card
- Built-in web dashboard at `http://<pi-ip>:8080/` — phone-friendly remote
  for pause / next / mode / brightness / overlay fields / clock
- CLI for ops:
  `immframe state`, `immframe pause`, `immframe next`,
  `immframe brightness 0.5`, `immframe mode smart`,
  `immframe query "sunsets"`, `immframe random 5`, etc.

## Status

**Phase 1 + 2 complete.** Slideshow with four switchable selection modes,
MQTT control with Home Assistant auto-discovery, REST API for monitoring
and command, and a built-in web dashboard. The frame is controlled entirely
via HA, HTTP, or the dashboard — there's no on-device input (no keyboard,
mouse, or touch).

151 unit tests across config, Immich client, selectors, prefetch worker,
MQTT, HTTP, and the controller.

## Quick start

```bash
git clone https://github.com/JameZUK/immframe.git
cd immframe
python3 -m venv .venv
.venv/bin/pip install -e .

mkdir -p ~/.config/immframe
cat > ~/.config/immframe/config.yaml <<'EOF'
immich:
  url: https://immich.example.local
  api_key: YOUR-IMMICH-API-KEY
EOF
chmod 600 ~/.config/immframe/config.yaml

.venv/bin/immframe
```

The single config file holds everything — Immich URL, API key, MQTT and
HTTP credentials. Any string value supports `${ENV_VAR}` substitution if
you'd rather keep secrets out of the file:

```yaml
immich:
  api_key: ${IMMICH_API_KEY}
```

See **[INSTALL.md](./INSTALL.md)** for the full setup including system
packages, Raspberry Pi notes, systemd unit and troubleshooting.

Every config knob — every YAML key, every `show_text` field, every viewer
option — is documented in **[docs/configuration.md](./docs/configuration.md)**.

If you want **video playback** (incl. live photos) on a Raspberry Pi,
you also need a Wayland compositor — see
**[docs/display-setup.md](./docs/display-setup.md)** for the labwc
setup. Without one, the slideshow works but video silently fails
because pi3d and MPV fight for the framebuffer.

## Acknowledgements

- **[picframe](https://github.com/helgeerbe/picframe)** — Helge Erbe, Paddy
  Gaunt, Jeff Godfrey. The render loop, the mat compositor, the on-screen
  overlays and the multi-backend display-power handling all survived intact
  in this project.
- **[pi3d](https://pi3d.github.io/)** — Paddy Gaunt et al. The OpenGL ES
  wrapper that makes hardware-accelerated rendering on the Pi tractable
  from Python.
- **[Immich](https://immich.app)** — the self-hosted photo platform this
  project depends on.
- **[python-mpv](https://github.com/jaseg/python-mpv)** — the saner
  alternative to picframe's VLC subprocess.

## License

MIT. See [LICENSE](./LICENSE) — picframe authors' attribution is preserved.

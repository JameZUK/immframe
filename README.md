# immframe

A picture-frame slideshow that streams photos and videos directly from an
[Immich](https://immich.app) server. Designed for a Raspberry Pi wired to a
TV / dedicated display, with optional Home Assistant integration via MQTT
(coming in Phase 2).

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

- Three selection modes, switchable at runtime:
  - **Random** — `POST /api/search/random` across the whole library
  - **Album** — random shuffle within one or more albums
  - **Smart** — CLIP search (e.g. *"family at the beach"*)
- Crossfades, blur edges, Ken Burns, optional mat compositing (from
  picframe's renderer, unchanged)
- Date / location overlay text — fields come straight from Immich, no
  EXIF parsing
- Direct video streaming via [python-mpv](https://github.com/jaseg/python-mpv)
  with KMS/DRM output on the Pi — no local download, no transcode
- Graceful degradation if Immich is briefly unavailable

## Status

**Phase 1** — slideshow + selection works. No control plane yet (no MQTT,
no HTTP control, no touch input). Set the mode in config, run, `Ctrl-C`
to quit.

**Phase 2** will add MQTT (Home Assistant discovery), HTTP control endpoints
and touch / mouse / keyboard input.

34 unit tests pass against the client, config loader, selectors and prefetch
worker.

## Quick start

```bash
git clone https://github.com/JameZUK/immframe.git
cd immframe
python3 -m venv .venv
.venv/bin/pip install -e .

mkdir -p ~/.config/immframe
printf '%s' 'YOUR-IMMICH-API-KEY' > ~/.config/immframe/api_key
cat > ~/.config/immframe/config.yaml <<'EOF'
immich:
  url: https://immich.example.local
  api_key_file: ~/.config/immframe/api_key
EOF

.venv/bin/immframe
```

See **[INSTALL.md](./INSTALL.md)** for the full setup including system
packages, Raspberry Pi notes, systemd unit and troubleshooting.

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

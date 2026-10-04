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

- Selection modes, switchable at runtime:
  - **Random** — `POST /api/search/random` across the whole library
  - **Favourites** — random within what you've starred in Immich (also `min_rating`, tag and album narrowing on any random entry)
  - **Album** — random shuffle within one or more albums
  - **Smart** — CLIP search (e.g. *"family at the beach"*)
  - **Scene** — picks a random label, shows ~25 photos from it, then
    rotates. Labels come from your library's cities (`/search/cities`)
    or the curated CLIP themes (*beach*, *mountain*, *wedding*, …) —
    `scene_source` chooses. Zero config.
  - **People** — a person at a time: yours by UUID, or auto-rotating
    through everyone named in Immich with enough photos
    (`people_min_photos`), optionally only starred people
  - **Memory** / **Recent** / **Playlist** — on-this-day, latest uploads,
    and a round-robin of any of the above (with per-entry collages)
- Crossfades, blur edges, Ken Burns, optional mat compositing (from
  picframe's renderer, unchanged); consecutive portraits are shown as a
  side-by-side pair instead of each alone on a 16:9 screen
- Date / location overlay text — fields come straight from Immich, no
  EXIF parsing
- Direct video streaming via [python-mpv](https://github.com/jaseg/python-mpv)
  with KMS/DRM output on the Pi — no local download, no transcode;
  hardware decoding (`hwdec: auto-copy` → the Pi's v4l2m2m decoder)
- Live / motion photos (iPhone Live Photos, Samsung / Pixel motion
  photos): the photo shows first, then the clip plays — **once, looped,
  bounced back and forth ("boomerang"), reversed, or not at all** — with
  configurable lead-in, run time or repeat count, speed (slow-mo to 4×),
  a freeze at each turnaround, and whether the photo returns afterwards;
  all adjustable live from the dashboard
- Never shows the same few photos on repeat: every mode draws fresh
  random samples (Immich's metadata search is fixed-order), hidden
  live-photo companion clips are filtered out, and blocked photos are
  dropped even after they've been queued
- Speaks Immich 3.2's structured search filters (and the older flat
  fields on earlier servers — detected from `/server/version`)
- Graceful degradation if Immich is unavailable: the frame keeps the last
  photo up, or — at boot, or after a long outage — shows a "Can't load
  photos from Immich — retrying" screen with how long and why, and the
  dashboard shows a banner; it carries on by itself when Immich is back
- Home Assistant integration via MQTT auto-discovery — see
  [docs/home-assistant.md](./docs/home-assistant.md) for entities and a
  ready-made Lovelace card
- Built-in web dashboard at `http://<pi-ip>:8080/` — a phone-first app with
  a proper sign-in page (persistent "keep me signed in" sessions, no
  browser password popup):
  - **Now**: the photo on the frame with a live countdown, ⏮ Previous /
    ⏯ Pause / ⏭ Next, and ♥ Favourite, ↻ Rotate, ⊘ Block and *Open in Immich*
  - **Timeline**: what's coming up next and everything recently shown —
    tap a photo to show it again or block it
  - **Blocked**: every photo you've blocked, filterable, with one-tap
    (or bulk) unblock that also un-archives it in Immich
  - **Controls**: mode, timing, brightness, screen power, clock, caption
    fields and collages
  - keyboard shortcuts on desktop (← → space F R), light and dark themes
- **Settings page** (`/config`) edits `config.yaml` from the phone: a
  playlist builder (add / reorder / per-mode options / collage), every
  common setting as a form, a raw-YAML tab for the rest, validation
  through the same loader the daemon uses, and *Save & restart*
- CLI for ops:
  `immframe doctor` (checks Immich + the kiosk and prints fixes),
  `immframe state`, `immframe pause`, `immframe next`, `immframe previous`,
  `immframe favorite`, `immframe rotate`, `immframe hide`,
  `immframe brightness 0.5`, `immframe mode smart`,
  `immframe query "sunsets"`, `immframe random 5`, etc.

## Status

In daily use on a Raspberry Pi 4 driving a 4K TV. Nine switchable
selection modes (one is a playlist mixing the others) and collages, MQTT control with Home
Assistant auto-discovery, a REST API, a web dashboard with sign-in, a
settings editor, and `immframe doctor` for diagnosis. The frame is
controlled entirely via HA, HTTP, or the dashboard — there's no on-device
input (no keyboard, mouse, or touch).

445 unit tests across config, the Immich client, selectors, prefetch
worker, controller, sessions, the block list, MQTT, HTTP and the doctor.

Parked ideas are written up in [docs/ideas/](./docs/ideas/) — e.g.
[ML orientation detection](./docs/ideas/ml-orientation.md) for photos
whose rotation metadata is missing.

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

To use the dashboard, enable it and set a login:

```yaml
control:
  http:
    enabled: true
    bind: 0.0.0.0          # reachable from the LAN
    username: you
    password: something-long
```

then open `http://<pi-ip>:8080/`. After that most settings can be changed
from the dashboard's Settings page.

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

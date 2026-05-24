# Installing immframe

← [Back to README](./README.md)

This guide covers running immframe on a Raspberry Pi (3B+ / 4 / 5 should all
work) against an existing [Immich](https://immich.app) server. The same
instructions work on a regular Linux desktop for development.

## Prerequisites

- A Raspberry Pi (or any Linux box for dev) running Debian 12+ / Raspberry
  Pi OS Bookworm or newer.
- Python **3.10+**.
- An [Immich](https://immich.app) server reachable from the Pi.
- An Immich API key:
  *Account Settings → API Keys → New API Key*.

## 1. System packages

```bash
sudo apt update
sudo apt install -y \
    git python3-venv python3-pip \
    libsdl2-2.0-0 libsdl2-image-2.0-0 \
    libmpv2
```

- `libsdl2-*` — required by [pi3d](https://pi3d.github.io/)'s display
  backend.
- `libmpv2` — required by [python-mpv](https://github.com/jaseg/python-mpv)
  for video playback. Missing libmpv is non-fatal: immframe still runs,
  video assets are skipped with a log warning.

> On older Debian / Raspberry Pi OS releases the libmpv package may be
> named `libmpv1`. Use whichever is in your apt sources.

## 2. Clone and install

```bash
git clone https://github.com/JameZUK/immframe.git
cd immframe
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

For development (tests, lint):

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## 3. Configure

Everything — Immich URL, API key, Phase-2 MQTT/HTTP credentials, viewer
options — lives in a single file: `~/.config/immframe/config.yaml`.

```bash
mkdir -p ~/.config/immframe
cat > ~/.config/immframe/config.yaml <<'EOF'
immich:
  url: https://immich.example.local        # your Immich server, no trailing slash
  api_key: YOUR-IMMICH-API-KEY             # required

selection:
  default_mode: random                     # random | album | smart | scene
  # album_ids: ["abc-123"]                  # when default_mode = album
  # smart_query: "family at the beach"      # when default_mode = smart
  # scene mode needs no config — it discovers labels via Immich's
  # /search/explore endpoint and rotates through them automatically

viewer:
  time_delay: 60                           # seconds per slide
  fade_time: 4
  show_text: "date location"               # "" to hide overlay
EOF
chmod 600 ~/.config/immframe/config.yaml
```

> **Important:** the config holds secrets — always `chmod 600`.

### Keeping secrets out of the file

If you'd rather not paste credentials into the YAML (e.g. for committing
config to a private repo, or feeding via systemd's `EnvironmentFile`), any
string value supports `${ENV_VAR}` substitution:

```yaml
immich:
  url: https://immich.example.local
  api_key: ${IMMICH_API_KEY}
```

Missing env vars expand to `""` (which will fail validation for required
fields like `api_key`, so you'll know immediately).

### Default keys

Everything else inherits from packaged defaults — see
[`src/immframe/_defaults/default.yaml`](./src/immframe/_defaults/default.yaml).

The full reference — every section, every key, every `show_text` field, every
viewer option, with types / defaults / what they actually do — is
[**docs/configuration.md**](./docs/configuration.md).

## 4. Run

```bash
cd ~/immframe
.venv/bin/immframe                         # or: .venv/bin/python -m immframe.start
```

Logs land on stdout. Flags:

| Flag | Effect |
|---|---|
| `--log-level DEBUG` | Verbose logging |
| `--config <path>` | Override config search |
| `--version` | Print version and exit |

`Ctrl-C` to quit. `SIGTERM` also clean-exits.

## 5. Control the frame

immframe exposes itself three ways. All are opt-in via the `control:`
section of the config file. The frame has no on-device input (no keyboard,
mouse, or touch), so you'll want at least one of these.

### MQTT + Home Assistant

```yaml
control:
  mqtt:
    enabled: true
    host: homeassistant.local
    port: 1883
    user: immframe
    password: ${MQTT_PASSWORD}             # or inline
    base_topic: immframe
```

HA picks up the device automatically via MQTT discovery — see
[docs/home-assistant.md](./docs/home-assistant.md) for the entity list,
the generic-camera setup to view the current image, and a ready-made
Lovelace card.

### HTTP REST API + built-in dashboard

```yaml
control:
  http:
    enabled: true
    bind: 127.0.0.1                        # 0.0.0.0 to expose on LAN
    port: 8080
    auth: true
    username: admin
    password: ${IMMFRAME_HTTP_PW}
```

A phone-friendly web dashboard is served at `http://<pi-ip>:8080/` — pause,
next, mode switch, brightness slider, overlay toggles, current image.

Endpoints for scripting / curl:

| Endpoint | Method | Body |
|---|---|---|
| `/healthz` | GET | (no auth) liveness probe |
| `/api/state` | GET | full state snapshot |
| `/api/paused` | POST | `{"value": bool}` |
| `/api/selection_mode` | POST | `{"value": "random"\|"album"\|"smart"\|"scene"}` |
| `/api/album_ids` | POST | `{"value": ["uuid", ...]}` |
| `/api/smart_query` | POST | `{"value": "..."}` |
| `/api/next` | POST | — |
| `/api/brightness` | POST | `{"value": 0.0-1.0}` |
| `/api/display_is_on` | POST | `{"value": bool}` |
| `/api/show_text` | POST | `{"value": ["title", "date", ...]}` |
| `/api/show_clock` | POST | `{"value": bool}` |
| `/api/time_delay` | POST | `{"value": seconds}` |
| `/api/fade_time` | POST | `{"value": seconds}` |
| `/api/image/<asset_id>` | GET | proxy preview JPEG from Immich |

```bash
curl -u admin:hunter2 http://127.0.0.1:8080/api/state | jq
curl -u admin:hunter2 -X POST http://127.0.0.1:8080/api/paused -d '{"value":true}'
```

### CLI

The `immframe` entry point doubles as a small CLI client when given a
subcommand. It reads the same config to find URL + credentials, so it
"just works" once `control.http.enabled: true`.

```bash
immframe state               # pretty-print /api/state
immframe pause / resume      # toggle pause
immframe next                # advance one slide
immframe mode scene          # random | album | smart | scene
immframe brightness 0.5
immframe display on|off
immframe albums "uuid-1,uuid-2"
immframe query "family at the beach"
immframe delay 30            # seconds per slide
immframe fade 1.5            # seconds for crossfade
immframe show-text title,date,location
immframe clock on|off
immframe immich-ping         # verify Immich reachable
immframe random 5            # list 5 random asset IDs from Immich
immframe explore             # dump Immich's /search/explore facets (scene-mode debug)
```

## 6. Run on boot (optional)

User-level systemd is the simplest path — no root needed for the service
itself:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/immframe.service <<'EOF'
[Unit]
Description=Immich picture frame
After=network-online.target

[Service]
ExecStart=%h/immframe/.venv/bin/immframe --log-level INFO
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now immframe
```

So it keeps running when you log out:

```bash
sudo loginctl enable-linger $USER
```

Follow logs:

```bash
journalctl --user -u immframe -f
```

## Updating

```bash
cd ~/immframe
git pull
.venv/bin/pip install -e .                 # picks up dep changes
systemctl --user restart immframe          # if running under systemd
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ImmichError: ... 401` | API key wrong or `immich.api_key` empty in config |
| `ImmichError: ... ConnectionError` | URL wrong, or Immich unreachable from the Pi |
| Black screen forever | No assets matched the current selection — try `--log-level DEBUG` |
| Scene mode produces no slides | Immich hasn't finished CLIP classification yet — check Immich → Administration → Jobs. Run `immframe explore` to see what facets Immich is exposing. |
| Dashboard / API responds 401 | Auth credentials mismatch between config and request |
| Dashboard unreachable | `control.http.bind` is `127.0.0.1` (default); set to `0.0.0.0` for LAN access |
| HA doesn't discover the device | `control.mqtt.enabled: true`?  broker reachable?  same broker as HA's MQTT integration? |
| `ImportError` for pi3d deps | Missing SDL2 system packages (step 1) |
| Videos black / silent | libmpv missing, or codec issue — try `mpv <url>` directly |
| Display wrong size | Set `viewer.display_w` / `viewer.display_h` in YAML |
| Multiple HDMI outputs | Set `viewer.display_hdmi: "HDMI-A-2"` |
| `permission denied` opening framebuffer | Add user to `video` group: `sudo usermod -aG video $USER`, log out / back in |

If something blows up, run with `--log-level DEBUG` and open an issue with
the log excerpt — please include your Pi model, OS version, Python version
and Immich version.

← [Back to README](./README.md)

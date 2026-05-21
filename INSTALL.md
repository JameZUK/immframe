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

```bash
mkdir -p ~/.config/immframe
printf '%s' 'YOUR-IMMICH-API-KEY' > ~/.config/immframe/api_key
chmod 600 ~/.config/immframe/api_key
```

Then `~/.config/immframe/config.yaml`:

```yaml
immich:
  url: https://immich.example.local        # your Immich server, no trailing slash
  api_key_file: ~/.config/immframe/api_key

selection:
  default_mode: random                     # random | album | smart
  # album_ids: ["abc-123"]                  # when default_mode = album
  # smart_query: "family at the beach"      # when default_mode = smart

viewer:
  time_delay: 60                           # seconds per slide
  fade_time: 4
  show_text: "date location"               # "" to hide overlay
```

Everything else inherits from packaged defaults — see
[`src/immframe/_defaults/default.yaml`](./src/immframe/_defaults/default.yaml)
for the immframe-level keys, and the
[picframe wiki](https://github.com/helgeerbe/picframe/wiki/Configuration)
for the full viewer key reference (blur, mat, clock, font, etc.).

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

## 5. Run on boot (optional)

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
| `ImmichError: ... 401` | API key wrong or `api_key_file` is empty |
| `ImmichError: ... ConnectionError` | URL wrong, or Immich unreachable from the Pi |
| Black screen forever | No assets matched the current selection — try `--log-level DEBUG` |
| `ImportError` for pi3d deps | Missing SDL2 system packages (step 1) |
| Videos black / silent | libmpv missing, or codec issue — try `mpv <url>` directly |
| Display wrong size | Set `viewer.display_w` / `viewer.display_h` in YAML |
| Multiple HDMI outputs | Set `viewer.display_hdmi: "HDMI-A-2"` |
| `permission denied` opening framebuffer | Add user to `video` group: `sudo usermod -aG video $USER`, log out / back in |

If something blows up, run with `--log-level DEBUG` and open an issue with
the log excerpt — please include your Pi model, OS version, Python version
and Immich version.

← [Back to README](./README.md)

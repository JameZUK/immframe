# Display setup on Raspberry Pi

immframe renders the slideshow via [pi3d](https://github.com/pi3d/pi3d)
and plays videos via [python-mpv](https://github.com/jaseg/python-mpv).
These are two separate GL/video clients. On Raspberry Pi, **both want
to drive the framebuffer**, and they can only coexist if a windowing
system (X11 or Wayland) is mediating between them.

Bare TTY + KMS-direct mode looks tempting (no compositor overhead!)
but it traps you: pi3d grabs the DRM CRTC via SDL2's KMSDRM backend,
MPV can't take it back, and video playback fails silently with
`Cannot set CRTC: Permission denied`.

This document is the cure.

← [Back to README](../README.md)

---

## TL;DR — automated install

```bash
cd ~/immframe
./scripts/setup-display.sh
# preview first if you want:
./scripts/setup-display.sh --dry-run
# full hands-off install:
./scripts/setup-display.sh --yes --reboot
```

The script is idempotent, backs up any file it overwrites, and verifies
the final state. It does steps 1-7 below for you. The rest of this
document explains what each step does — read it if you want to know
what's happening or to do it by hand.

## TL;DR — manual

```
RPi OS Bookworm + labwc + autologin + autostart immframe
```

labwc is the **official default Wayland compositor on Raspberry Pi OS
Bookworm/Trixie** (switched from wayfire in Oct 2024). It's lightweight,
adds ~3-5 seconds to boot, and gives you hardware video overlay
("direct scanout") for free — meaning fullscreen video plays through the
same fast KMS-plane path as `vo=drm`, without the DRM-master fight.

If you already run RPi OS Bookworm with the desktop, you're already
on labwc — skip to [§4 Autostart immframe](#4-autostart-immframe).

---

## 1. Install labwc

If you don't already have it:

```bash
sudo apt update
sudo apt install -y labwc seatd
```

`seatd` brokers access to `/dev/dri/*` and `/dev/input/*` so labwc can
run without being root. On RPi OS Bookworm it should already be present;
install it explicitly if you're on a different distro.

Verify the user is in the `video` and `input` groups:

```bash
sudo usermod -aG video,input,seat $USER
# Log out / log back in for group changes to take effect.
```

---

## 2. Configure autologin to TTY1

```bash
sudo raspi-config
# → 1 System Options
# → S5 Boot / Auto Login
# → B2 Console Autologin
# → Finish, reboot
```

Or manually with systemd:

```bash
sudo systemctl edit getty@tty1.service
```

…and paste:

```ini
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin pi --noclear %I $TERM
```

(Replace `pi` with whatever your username is.)

Reboot:

```bash
sudo reboot
```

After boot, you should log in to `/dev/tty1` automatically without a
password prompt.

---

## 3. Launch labwc from TTY1 on login

Add to the **bottom** of `~/.bash_profile` (or `~/.profile` if you use
that):

```bash
# Auto-launch labwc on TTY1
if [ "$(tty)" = "/dev/tty1" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    exec dbus-run-session labwc
fi
```

`dbus-run-session` gives labwc a per-session DBus, which keeps
PulseAudio / portals / notifications happy. The `exec` means labwc
replaces your shell — when labwc exits, you're back at a login prompt.

Reboot once more and you should land in a black labwc screen (no
panel, no wallpaper — labwc is purely a window manager, nothing else).

---

## 4. Autostart immframe

Create the labwc config dir and autostart file:

```bash
mkdir -p ~/.config/labwc
```

`~/.config/labwc/autostart`:

```sh
#!/bin/sh
# Hide the cursor when idle (we have no mouse on a frame)
# Requires `wlopm` or similar; harmless if missing.
# Power off the display via DPMS after N seconds of idle:
# (uncomment if you want — picframe-style display power is also
#  controllable via HA / HTTP)
# swayidle -w timeout 600 'wlopm --off "*"' resume 'wlopm --on "*"' &

# Start the slideshow
~/immframe/.venv/bin/immframe &
```

Make it executable:

```bash
chmod +x ~/.config/labwc/autostart
```

`~/.config/labwc/rc.xml`:

```xml
<?xml version="1.0"?>
<labwc_config>
  <core>
    <gap>0</gap>
  </core>
  <theme>
    <cornerRadius>0</cornerRadius>
    <titlebar show="no"/>
  </theme>
  <windowRules>
    <!-- Make immframe (and any pi3d-SDL2 window) fullscreen + undecorated. -->
    <windowRule identifier="*" matchOnce="false">
      <ignoreFocusRequest>false</ignoreFocusRequest>
      <action name="ToggleFullscreen"/>
    </windowRule>
  </windowRules>
</labwc_config>
```

(The `<windowRule identifier="*">` makes every new window fullscreen —
fine on a kiosk that only ever runs one or two apps. If you'd rather
target only immframe / MPV specifically, replace `"*"` with the actual
identifier shown by `swaymsg -t get_tree` or labwc's debug output.)

> **Important — leave `video.fullscreen: false` (the default).** Because
> this rule fullscreens *every* window, MPV must **not** also request
> fullscreen. If it does, the rule toggles MPV's already-fullscreen window
> back to a tiny default-size one — videos then play in a small box in the
> corner while the slideshow stays fullscreen. immframe ships with
> `video.fullscreen: false` so the compositor owns fullscreen for both the
> pi3d window and MPV. Only set `video.fullscreen: true` if you run immframe
> under a compositor that does *not* auto-fullscreen windows.

Reboot. You should see the slideshow appear within a few seconds of
the TTY login.

---

## 5. Verify video works

The previous DRM-master fight is gone — pi3d and MPV are both regular
Wayland clients now, both rendering through wlroots' GL pipeline.
Fullscreen video gets KMS-plane direct scanout automatically, so
playback is hardware-accelerated even on the Pi 4.

Quick test (from another machine, via SSH):

```bash
~/immframe/.venv/bin/immframe mode random
# Force-advance until a video comes up:
for i in 1 2 3 4 5 6 7 8 9 10; do
  ~/immframe/.venv/bin/immframe next
  kind=$(~/immframe/.venv/bin/immframe state | python3 -c "import sys,json; print(json.load(sys.stdin)['current_asset']['kind'])")
  echo "$i: $kind"
  [ "$kind" = "VIDEO" ] && break
  sleep 3
done
```

Or watch the journal as a video comes up:

```bash
journalctl --user -u immframe -f
# Look for: "video play: asset=… url=…" and "MPV ready: vo='gpu' …"
# If you see no MPV warnings, video is working.
```

---

## 6. Alternatives

### Use the full RPi OS desktop

If you already run the desktop, you have labwc. Just install immframe
under your normal user, autostart it from `~/.config/labwc/autostart`
or via `~/.config/autostart/immframe.desktop`, and you're done. Slightly
heavier (panels, wallpaper service), but easier to onboard.

### cage (single-app kiosk)

[cage](https://github.com/cage-kiosk/cage) is a minimal wlroots
compositor that runs exactly one application fullscreen. **Don't use
it** — immframe needs to spawn MPV as a second client, and cage
deliberately allows only one.

### wayfire

The previous Pi default. Heavier than labwc, panel-by-default,
desktop-like. labwc replaced it. No reason to choose wayfire over
labwc on a fresh install.

### Stay on bare TTY (no compositor)

The path you're on now. Slideshow works, **video playback does not**
and never will without a compositor — the DRM-master conflict is
structural. If video is truly not needed, set `video.enabled: false`
in `~/.config/immframe/config.yaml` and ignore the warning at startup.

---

## 7. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `[vo/drm] Cannot set CRTC: Permission denied` in MPV logs | You're still on bare TTY; labwc isn't running. Verify `WAYLAND_DISPLAY` is set inside the immframe session. |
| labwc starts but screen stays black | Check `WAYLAND_DISPLAY` is set when `immframe` starts (autostart runs inside labwc's session, so it should be). If running immframe via SSH for testing, prefix with `WAYLAND_DISPLAY=wayland-0 immframe`. |
| Cursor visible | `unclutter` or `swayidle`-driven `wlopm` can hide it. Or use `<cursor showOnPointer="false"/>` in labwc theme (newer labwc). |
| immframe window not fullscreen | Adjust the `<windowRule>` `identifier` to match what your pi3d build sets — run `swaymsg -t get_tree` in another terminal to see live identifiers. |
| Boots into TTY not labwc | `tty` returns something other than `/dev/tty1` (you might be on tty2). Adjust the check in `~/.bash_profile`. |
| Video has audio but no picture | Check the `video.vo` config key — default `gpu` is right for labwc; only set to `drm` if you're explicitly on bare TTY (and even then you'll hit the conflict). |

---

← [Back to README](../README.md) · [Configuration reference →](./configuration.md)

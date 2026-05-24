#!/usr/bin/env bash
# diagnose-display.sh — figure out why labwc isn't running on a Pi that
# should have it autostarted (after setup-display.sh + reboot).
#
# Self-contained dump. Mirrors output to /tmp/immframe-display.log so you
# can paste it back in one go.

LOG=/tmp/immframe-display.log
exec > >(tee "$LOG") 2>&1

section() { printf '\n=== %s ===\n' "$*"; }

section "A. uptime / boot"
uptime
who -b

section "B. all login sessions"
loginctl list-sessions
for s in $(loginctl list-sessions --no-legend | awk '{print $1}'); do
    echo "--- session $s ---"
    loginctl show-session "$s" 2>/dev/null \
        | grep -E "^(Name|TTY|Type|Class|Active|State|Service|Display)=" \
        || true
done

section "C. processes that should be running"
ps -ef | grep -E "labwc|dbus-run-session|getty|immframe" | grep -v grep || echo "(none matched)"

section "D. autologin drop-in"
sudo cat /etc/systemd/system/getty@tty1.service.d/immframe-autologin.conf 2>&1

section "E. bash startup files in \$HOME"
ls -la ~/.bash_profile ~/.bash_login ~/.profile ~/.bashrc 2>/dev/null || true
echo "--- bash_profile labwc block ---"
grep -A6 "immframe-display-launcher" ~/.bash_profile 2>/dev/null \
    || echo "(no immframe-display-launcher block found)"

section "F. boot logs for labwc + autologin"
journalctl -b --no-pager 2>/dev/null \
    | grep -iE "labwc|getty@tty1|autologin|wayland|wlroots" \
    | tail -40 \
    || echo "(no matching lines)"

section "G. how immframe got launched"
systemctl --user status immframe --no-pager 2>&1 | head -25

section "H. is the systemd unit really disabled?"
systemctl --user is-enabled immframe 2>&1
echo "--- ~/.config/systemd/user/default.target.wants/ ---"
ls -la ~/.config/systemd/user/default.target.wants/ 2>&1 || true

section "I. what's actually on /dev/tty1?"
sudo head -c 1024 /dev/vcs1 2>/dev/null | tr -d '\0' | head -10 \
    || echo "(can't read /dev/vcs1 — needs sudo)"

section "Done"
echo "Full log: $LOG"
echo
echo "Paste the contents of $LOG back in chat. One command:"
echo "  cat $LOG"

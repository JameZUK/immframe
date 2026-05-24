#!/usr/bin/env bash
# setup-display.sh — one-shot installer for labwc + autologin + immframe
# autostart on a Raspberry Pi running RPi OS Bookworm or newer.
#
# Idempotent: safe to re-run. Detects existing state, only changes what
# needs changing, backs up anything it overwrites.
#
# Usage:
#   scripts/setup-display.sh                    # interactive (default)
#   scripts/setup-display.sh --dry-run          # show what would change, change nothing
#   scripts/setup-display.sh --yes              # auto-confirm prompts
#   scripts/setup-display.sh --yes --reboot     # full hands-off install + reboot
#   scripts/setup-display.sh --user pi          # override target user
#
# What it does:
#   1.  Sanity checks (Bookworm or newer, not root, sudo available, immframe
#       checkout reachable)
#   2.  Installs labwc + seatd via apt (idempotent)
#   3.  Adds the target user to the video / input / seat groups
#   4.  Enables and starts seatd (the DRM/input broker)
#   5.  Configures autologin on tty1 via a systemd drop-in
#   6.  Adds the labwc-launch block to ~/.bash_profile (marker-fenced so
#       repeat runs update in place)
#   7.  Copies labwc autostart + rc.xml from examples/labwc/ (backs up any
#       existing ones)
#   8.  Disables the existing immframe systemd user service (autostart
#       takes over inside labwc)
#   9.  Prints a verification summary + suggests reboot

set -uo pipefail

# ── State ──────────────────────────────────────────────────────────────────
DRY_RUN=0
YES=0
DO_REBOOT=0
TARGET_USER=""

CHANGES=()      # list of "[ACTION] description" lines for summary
SKIPPED=()      # list of "already configured" lines
ERRORS=()       # list of failures

# ── Colored output ─────────────────────────────────────────────────────────
if [ -t 1 ]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YLW=$'\033[33m'
    C_BLU=$'\033[34m'; C_DIM=$'\033[2m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YLW=""; C_BLU=""; C_DIM=""; C_BLD=""; C_RST=""
fi

section() { printf '\n%s=== %s ===%s\n' "$C_BLD" "$*" "$C_RST"; }
ok()      { printf '  %sOK%s    %s\n' "$C_GRN" "$C_RST" "$*"; }
fail()    { printf '  %sFAIL%s  %s\n' "$C_RED" "$C_RST" "$*"; ERRORS+=("$*"); }
warn()    { printf '  %sWARN%s  %s\n' "$C_YLW" "$C_RST" "$*"; }
info()    { printf '        %s\n' "$*"; }
done_()   { CHANGES+=("$*"); printf '  %sDID%s   %s\n' "$C_BLU" "$C_RST" "$*"; }
skip()    { SKIPPED+=("$*"); printf '  %s--%s    %s (already configured)\n' "$C_DIM" "$C_RST" "$*"; }
dry()     { printf '  %sDRY%s   would: %s\n' "$C_YLW" "$C_RST" "$*"; CHANGES+=("[would] $*"); }

abort() {
    printf '\n%sAborting:%s %s\n' "$C_RED$C_BLD" "$C_RST" "$*"
    exit 1
}

# ── Helpers ────────────────────────────────────────────────────────────────
# Run a shell command, respecting dry-run. Returns 0 on success.
run() {
    if [ "$DRY_RUN" = "1" ]; then
        dry "$*"
        return 0
    fi
    if eval "$*"; then
        return 0
    else
        fail "command failed: $*"
        return 1
    fi
}

# Run a shell command with sudo, respecting dry-run.
run_sudo() {
    if [ "$DRY_RUN" = "1" ]; then
        dry "sudo $*"
        return 0
    fi
    if sudo bash -c "$*"; then
        return 0
    else
        fail "sudo command failed: $*"
        return 1
    fi
}

# Append a line to a file (with sudo) only if not present.
ensure_line() {
    local line=$1 file=$2 use_sudo=${3:-0}
    if [ "$use_sudo" = "1" ]; then
        if sudo grep -qxF "$line" "$file" 2>/dev/null; then
            return 1   # already present
        fi
        run_sudo "echo $(printf '%q' "$line") >> $(printf '%q' "$file")"
    else
        if grep -qxF "$line" "$file" 2>/dev/null; then
            return 1
        fi
        run "echo $(printf '%q' "$line") >> $(printf '%q' "$file")"
    fi
    return 0
}

# Back up a file before overwriting; only if file exists AND content differs
# from the new content.
backup_if_differs() {
    local existing=$1 new=$2
    if [ -f "$existing" ] && cmp -s "$existing" "$new" 2>/dev/null; then
        return 1   # identical
    fi
    if [ -f "$existing" ]; then
        local backup="${existing}.bak-$(date +%Y%m%d-%H%M%S)"
        run "cp -p $(printf '%q' "$existing") $(printf '%q' "$backup")" \
            && info "backed up to $backup"
    fi
    return 0
}

# Marker-fenced block replacement in a user-editable file (~/.bash_profile)
# Removes any existing block between BEGIN/END markers, then appends new one.
upsert_marked_block() {
    local file=$1 marker=$2 content=$3
    local begin="# >>> ${marker} >>>"
    local end="# <<< ${marker} <<<"

    # If file exists and an identical block is already present, no-op.
    if [ -f "$file" ] && awk -v b="$begin" -v e="$end" '
        $0==b { in_block=1; next }
        $0==e { in_block=0; next }
        in_block { print }
    ' "$file" | diff -q - <(printf '%s\n' "$content") >/dev/null 2>&1; then
        return 1   # already up-to-date
    fi

    if [ "$DRY_RUN" = "1" ]; then
        dry "upsert ${marker} block in $file"
        return 0
    fi

    # Back up
    if [ -f "$file" ]; then
        cp -p "$file" "${file}.bak-$(date +%Y%m%d-%H%M%S)"
    fi
    # Remove any existing fenced block then append new one
    if [ -f "$file" ]; then
        local tmp; tmp=$(mktemp)
        awk -v b="$begin" -v e="$end" '
            $0==b { skip=1; next }
            $0==e { skip=0; next }
            !skip { print }
        ' "$file" > "$tmp"
        mv "$tmp" "$file"
    fi
    {
        echo ""
        echo "$begin"
        printf '%s\n' "$content"
        echo "$end"
    } >> "$file"
    return 0
}

# ── Args ───────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case $1 in
        --dry-run) DRY_RUN=1 ;;
        --yes|-y)  YES=1 ;;
        --reboot)  DO_REBOOT=1 ;;
        --user)    TARGET_USER=${2:-}; shift ;;
        -h|--help)
            sed -n '/^# setup-display.sh/,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) abort "unknown arg: $1" ;;
    esac
    shift
done

# ── Resolve invoking user ──────────────────────────────────────────────────
if [ -z "$TARGET_USER" ]; then
    # If run via sudo, prefer the original user
    TARGET_USER="${SUDO_USER:-$USER}"
fi
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
if [ -z "$TARGET_HOME" ] || [ ! -d "$TARGET_HOME" ]; then
    abort "could not resolve home dir for user '$TARGET_USER'"
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
IMMFRAME_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
EXAMPLES="$IMMFRAME_DIR/examples/labwc"

# ── Banner ─────────────────────────────────────────────────────────────────
printf '%simmframe display setup%s — labwc + autologin + autostart\n' "$C_BLD" "$C_RST"
printf '  Target user : %s\n' "$TARGET_USER"
printf '  Home dir    : %s\n' "$TARGET_HOME"
printf '  immframe at : %s\n' "$IMMFRAME_DIR"
[ "$DRY_RUN" = "1" ] && printf '  Mode        : %sDRY-RUN%s (no changes)\n' "$C_YLW" "$C_RST"

# ── Preflight ──────────────────────────────────────────────────────────────
section "Preflight checks"

# Don't run as root — we want the invoking user's $HOME for configs.
if [ "$(id -u)" = "0" ] && [ -z "${SUDO_USER:-}" ]; then
    abort "do not run this script as root directly. Run as your normal user; the script will sudo when needed."
fi
ok "running as $USER (target=$TARGET_USER)"

# OS sanity
if [ -r /etc/os-release ]; then
    . /etc/os-release
    case "${ID:-}" in
        debian|raspbian) ok "OS: $PRETTY_NAME" ;;
        *) warn "OS: $PRETTY_NAME — script tested on Debian/Raspbian Bookworm" ;;
    esac
    if [ -n "${VERSION_ID:-}" ] && [ "${VERSION_ID%%.*}" -lt 12 ] 2>/dev/null; then
        warn "OS version $VERSION_ID is older than Bookworm — labwc may not be in apt"
    fi
else
    warn "could not detect OS"
fi

# Sudo available?
if ! command -v sudo >/dev/null 2>&1; then
    abort "sudo not installed — script needs sudo for apt + getty drop-in"
fi
if ! sudo -n true 2>/dev/null; then
    info "you may be prompted for your sudo password during install"
fi
ok "sudo available"

# Examples present?
if [ ! -d "$EXAMPLES" ]; then
    abort "examples dir missing at $EXAMPLES — are you running this from the immframe checkout?"
fi
ok "examples found at $EXAMPLES"

# apt-get present?
if ! command -v apt-get >/dev/null 2>&1; then
    abort "apt-get not found — this script is Debian/Raspbian-specific"
fi

# ── Plan ───────────────────────────────────────────────────────────────────
section "Plan"
cat <<EOF
  1. Install labwc + seatd via apt
  2. Add '$TARGET_USER' to the video, input, seat groups
  3. Enable + start seatd
  4. Configure autologin for '$TARGET_USER' on tty1
  5. Add labwc-launch block to ${TARGET_HOME}/.bash_profile
  6. Copy labwc autostart + rc.xml to ${TARGET_HOME}/.config/labwc/
  7. Disable the immframe systemd user service (autostart will replace it)
EOF

if [ "$YES" != "1" ] && [ "$DRY_RUN" != "1" ]; then
    printf '\n%sProceed?%s [y/N] ' "$C_BLD" "$C_RST"
    read -r ans
    case "$ans" in
        y|Y|yes|YES) : ;;
        *) abort "cancelled by user" ;;
    esac
fi

# ── 1. Packages ────────────────────────────────────────────────────────────
section "1. Packages (labwc + seatd)"
NEED_INSTALL=()
for pkg in labwc seatd; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        skip "$pkg already installed"
    else
        NEED_INSTALL+=("$pkg")
    fi
done
if [ ${#NEED_INSTALL[@]} -gt 0 ]; then
    run_sudo "apt-get update" \
        && run_sudo "DEBIAN_FRONTEND=noninteractive apt-get install -y ${NEED_INSTALL[*]}" \
        && done_ "installed: ${NEED_INSTALL[*]}"
fi

# ── 2. Groups ──────────────────────────────────────────────────────────────
section "2. Groups (video, input, seat)"
NEED_GROUPS=()
for grp in video input seat; do
    # Some distros don't have a 'seat' group; check existence first
    if ! getent group "$grp" >/dev/null 2>&1; then
        warn "group '$grp' does not exist — skipping"
        continue
    fi
    if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx "$grp"; then
        skip "$TARGET_USER already in $grp"
    else
        NEED_GROUPS+=("$grp")
    fi
done
if [ ${#NEED_GROUPS[@]} -gt 0 ]; then
    run_sudo "usermod -aG $(IFS=,; echo "${NEED_GROUPS[*]}") $TARGET_USER" \
        && done_ "added $TARGET_USER to: ${NEED_GROUPS[*]} (re-login required)"
fi

# ── 3. seatd service ───────────────────────────────────────────────────────
section "3. seatd service"
if systemctl is-enabled seatd >/dev/null 2>&1; then
    skip "seatd already enabled"
else
    run_sudo "systemctl enable seatd" && done_ "enabled seatd"
fi
if systemctl is-active seatd >/dev/null 2>&1; then
    skip "seatd already running"
else
    run_sudo "systemctl start seatd" && done_ "started seatd"
fi

# ── 4. Autologin on tty1 ───────────────────────────────────────────────────
section "4. tty1 autologin"
DROPIN_DIR=/etc/systemd/system/getty@tty1.service.d
DROPIN=$DROPIN_DIR/immframe-autologin.conf
DROPIN_BODY=$(cat <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $TARGET_USER --noclear %I \$TERM
EOF
)
NEEDS_DROPIN=1
if [ -f "$DROPIN" ]; then
    if diff -q <(printf '%s\n' "$DROPIN_BODY") "$DROPIN" >/dev/null 2>&1; then
        skip "autologin drop-in already present for $TARGET_USER"
        NEEDS_DROPIN=0
    fi
fi
if [ "$NEEDS_DROPIN" = "1" ]; then
    if [ "$DRY_RUN" = "1" ]; then
        dry "write $DROPIN with autologin user=$TARGET_USER"
    else
        sudo install -d -m 755 "$DROPIN_DIR"
        printf '%s\n' "$DROPIN_BODY" | sudo tee "$DROPIN" >/dev/null
        run_sudo "systemctl daemon-reload" \
            && done_ "wrote $DROPIN for user=$TARGET_USER"
    fi
fi

# ── 5. ~/.bash_profile labwc launcher ──────────────────────────────────────
section "5. ${TARGET_HOME}/.bash_profile"
PROFILE=${TARGET_HOME}/.bash_profile
LAUNCHER=$(cat <<'EOF'
# Auto-launch labwc when logging in on tty1 (set up by immframe setup-display.sh)
if [ "$(tty)" = "/dev/tty1" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    exec dbus-run-session labwc
fi
EOF
)
# Ensure profile exists with right owner
if [ ! -f "$PROFILE" ]; then
    if [ "$DRY_RUN" = "1" ]; then
        dry "create $PROFILE"
    else
        touch "$PROFILE"
        chown "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$PROFILE"
    fi
fi
if upsert_marked_block "$PROFILE" "immframe-display-launcher" "$LAUNCHER"; then
    done_ "labwc launcher block written to $PROFILE"
else
    skip "labwc launcher block already in $PROFILE"
fi

# ── 6. labwc config files ──────────────────────────────────────────────────
section "6. labwc configs"
LABWC_DIR=${TARGET_HOME}/.config/labwc
if [ ! -d "$LABWC_DIR" ]; then
    if [ "$DRY_RUN" = "1" ]; then
        dry "create $LABWC_DIR"
    else
        sudo -u "$TARGET_USER" mkdir -p "$LABWC_DIR"
        done_ "created $LABWC_DIR"
    fi
fi

for name in autostart rc.xml; do
    SRC=$EXAMPLES/$name
    DST=$LABWC_DIR/$name
    if [ ! -f "$SRC" ]; then
        fail "example missing: $SRC"
        continue
    fi
    if cmp -s "$SRC" "$DST" 2>/dev/null; then
        skip "$DST already matches example"
        continue
    fi
    if [ -f "$DST" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            dry "back up + replace $DST"
        else
            BACKUP="${DST}.bak-$(date +%Y%m%d-%H%M%S)"
            cp -p "$DST" "$BACKUP"
            info "backed up existing $DST -> $BACKUP"
        fi
    fi
    if [ "$DRY_RUN" = "1" ]; then
        dry "install $SRC -> $DST"
    else
        install -m 644 "$SRC" "$DST"
        chown "$TARGET_USER":"$(id -gn "$TARGET_USER")" "$DST"
        [ "$name" = "autostart" ] && chmod +x "$DST"
        done_ "installed $DST"
    fi
done

# ── 7. Disable existing user systemd unit for immframe ─────────────────────
section "7. Disable user systemd immframe service"
UNIT_PATH=${TARGET_HOME}/.config/systemd/user/immframe.service
if [ ! -f "$UNIT_PATH" ]; then
    skip "no user systemd unit at $UNIT_PATH"
else
    # Try as the target user. If we're not them, use sudo -u.
    if [ "$USER" = "$TARGET_USER" ]; then
        SCTL="systemctl --user"
    else
        # Need the user's runtime dir + XDG_RUNTIME_DIR
        UID_=$(id -u "$TARGET_USER")
        SCTL="sudo -u $TARGET_USER XDG_RUNTIME_DIR=/run/user/$UID_ systemctl --user"
    fi
    if $SCTL is-active immframe >/dev/null 2>&1; then
        run "$SCTL stop immframe" && done_ "stopped immframe (was running as user service)"
    else
        skip "immframe user service not active"
    fi
    if $SCTL is-enabled immframe >/dev/null 2>&1; then
        run "$SCTL disable immframe" && done_ "disabled immframe user service (labwc autostart will launch it)"
    else
        skip "immframe user service not enabled"
    fi
fi

# ── Summary ────────────────────────────────────────────────────────────────
section "Summary"
printf '  Changes : %d\n' "${#CHANGES[@]}"
printf '  Skipped : %d (already configured)\n' "${#SKIPPED[@]}"
printf '  Errors  : %d\n' "${#ERRORS[@]}"
if [ ${#CHANGES[@]} -gt 0 ]; then
    printf '\n%sChanges this run:%s\n' "$C_BLD" "$C_RST"
    for c in "${CHANGES[@]}"; do printf '  - %s\n' "$c"; done
fi
if [ ${#ERRORS[@]} -gt 0 ]; then
    printf '\n%sErrors:%s\n' "$C_RED$C_BLD" "$C_RST"
    for e in "${ERRORS[@]}"; do printf '  - %s\n' "$e"; done
    exit 1
fi

# ── Verification ───────────────────────────────────────────────────────────
section "Verification"
[ -x "$(command -v labwc)" ] && ok "labwc binary available" || fail "labwc not on PATH"
systemctl is-active --quiet seatd && ok "seatd active" || fail "seatd not active"
id -nG "$TARGET_USER" | grep -qw video && ok "$TARGET_USER in video group" \
    || warn "$TARGET_USER not yet in video group (relogin needed)"
[ -f "$DROPIN" ] && ok "autologin drop-in present at $DROPIN" || warn "autologin drop-in missing"
[ -f "$LABWC_DIR/autostart" ] && ok "labwc autostart present" || warn "labwc autostart missing"
[ -f "$LABWC_DIR/rc.xml" ] && ok "labwc rc.xml present" || warn "labwc rc.xml missing"

# ── Next steps ─────────────────────────────────────────────────────────────
section "Next steps"
if [ "$DRY_RUN" = "1" ]; then
    info "this was a dry run — re-run without --dry-run to apply changes."
    exit 0
fi

cat <<EOF
A reboot is required for group membership + autologin to take effect.

After reboot you should land in labwc (a black screen with the immframe
slideshow appearing on top within a few seconds). Video playback should
now work — pi3d and MPV become regular Wayland clients sharing
wlroots' direct-scanout pipeline.

To verify (from SSH after reboot):

  journalctl --user-unit=labwc -n 30 --no-pager 2>/dev/null \
    || journalctl -t labwc -n 30 --no-pager
  pgrep -fa immframe

To force a video slide for testing:

  ${IMMFRAME_DIR}/.venv/bin/immframe mode random
  for i in 1 2 3 4 5 6 7 8 9 10; do
      ${IMMFRAME_DIR}/.venv/bin/immframe next
      sleep 3
  done
EOF

if [ "$DO_REBOOT" = "1" ]; then
    printf '\n%sRebooting in 5 seconds...%s (Ctrl-C to cancel)\n' "$C_YLW$C_BLD" "$C_RST"
    sleep 5
    sudo reboot
elif [ "$YES" != "1" ]; then
    printf '\n%sReboot now?%s [y/N] ' "$C_BLD" "$C_RST"
    read -r ans
    case "$ans" in
        y|Y|yes|YES) sudo reboot ;;
        *) info "reboot when ready: sudo reboot" ;;
    esac
else
    info "reboot when ready: sudo reboot"
fi

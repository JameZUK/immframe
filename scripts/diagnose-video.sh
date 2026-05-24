#!/usr/bin/env bash
# diagnose-video.sh — end-to-end check of immframe's video playback path.
#
# Usage:
#   IMMICH_URL=https://photos.example.com IMMICH_KEY=xxx ./diagnose-video.sh
#   ./diagnose-video.sh https://photos.example.com xxx
#
# What it checks, in order:
#   1.  Auth (calls /api/users/me)
#   2.  Finds a real video asset in your library
#   3.  Tests the /video/playback URL with curl
#   4.  Headless MPV decode of the same URL — proves MPV can decode it
#       without needing a display
#   5.  Finds a Live Photo (image + motion clip)
#   6.  Display / compositor environment (X11? Wayland? KMS-direct?)
#   7.  immframe systemd-user environment
#   8.  Optional: forces the slideshow to play a video and captures the
#       journalctl excerpt so we can see what immframe reports
#
# Designed to be safe to run repeatedly. Writes its own log to
# /tmp/immframe-diagnose.log so you can paste it back without scrolling.

set -u
LOG=/tmp/immframe-diagnose.log
exec > >(tee "$LOG") 2>&1

# ── Args ───────────────────────────────────────────────────────────────────
URL="${IMMICH_URL:-${1:-}}"
KEY="${IMMICH_KEY:-${2:-}}"
if [ -z "$URL" ] || [ -z "$KEY" ]; then
    echo "Usage: $0 <immich-url> <api-key>"
    echo "  or:  IMMICH_URL=... IMMICH_KEY=... $0"
    exit 2
fi
URL="${URL%/}"            # strip trailing slash
export URL KEY            # so the python heredocs see them

# ── Helpers ────────────────────────────────────────────────────────────────
section() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
ok()      { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
fail()    { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
info()    { printf '       %s\n' "$*"; }

require() {
    if ! command -v "$1" >/dev/null 2>&1; then
        fail "$1 not installed; some checks will be skipped"
        return 1
    fi
    return 0
}

# ── 1. Auth ────────────────────────────────────────────────────────────────
section "1. Auth"
WHOAMI=$(curl -sS -H "x-api-key: $KEY" "$URL/api/users/me" -w "\n__HTTP=%{http_code}")
HTTP=$(echo "$WHOAMI" | grep -oE 'HTTP=[0-9]+$' | head -1 | cut -d= -f2)
BODY=$(echo "$WHOAMI" | sed '$d')
if [ "$HTTP" = "200" ]; then
    EMAIL=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('email', '?'))" 2>/dev/null)
    NAME=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('name', '?'))" 2>/dev/null)
    ok "authenticated as $NAME <$EMAIL>"
else
    fail "auth check returned HTTP $HTTP"
    info "$BODY"
    info "Stopping — fix auth before continuing."
    exit 1
fi

# ── 2. Find a video ────────────────────────────────────────────────────────
section "2. Find a video asset"
VID=$(curl -sS -X POST -H "x-api-key: $KEY" -H "Content-Type: application/json" \
        -d '{"size":1,"type":"VIDEO","withExif":true}' "$URL/api/search/random" \
      | python3 -c "
import sys, json
d = json.load(sys.stdin)
if isinstance(d, list) and d:
    a = d[0]
    print(a['id'], a.get('originalFileName',''), a.get('originalMimeType',''), sep='|')
" 2>/dev/null)
if [ -z "$VID" ]; then
    fail "no video assets returned by /search/random"
    info "Your library may not contain videos, or video type isn't being indexed."
    exit 1
fi
ID="${VID%%|*}"; rest="${VID#*|}"
FNAME="${rest%%|*}"; MIME="${rest#*|}"
ok "id=$ID"
info "file=$FNAME"
info "mime=$MIME"

# ── 3. Video URL ───────────────────────────────────────────────────────────
section "3. /video/playback URL"
curl -sS -H "x-api-key: $KEY" -o /tmp/diag-v.bin --max-time 30 \
     "$URL/api/assets/$ID/video/playback" \
     -w "  HTTP=%{http_code}  ct=%{content_type}  bytes=%{size_download}\n" \
   || fail "curl returned non-zero"
FILE_TYPE=$(file -b /tmp/diag-v.bin 2>/dev/null | head -c 80)
info "file says: $FILE_TYPE"
if file /tmp/diag-v.bin | grep -qE "ISO Media|MP4|QuickTime|Matroska|WebM|MPEG"; then
    ok "returned actual video bytes"
else
    fail "did NOT return video — likely JSON error body:"
    head -c 300 /tmp/diag-v.bin
    echo
fi

# ── 4. MPV headless decode ─────────────────────────────────────────────────
section "4. MPV headless decode"
if ! require mpv; then
    info "skipping — install with: sudo apt install libmpv2 mpv"
else
    info "$(mpv --version 2>&1 | head -1)"
    LOG_MPV=/tmp/diag-mpv.log
    mpv --vo=null --ao=null --frames=30 --no-config --really-quiet \
        --log-file="$LOG_MPV" \
        --http-header-fields="x-api-key: $KEY" \
        "$URL/api/assets/$ID/video/playback" 2>&1 | head -20
    EXIT=${PIPESTATUS[0]}
    if [ "$EXIT" = "0" ]; then
        ok "MPV decoded the stream without a display"
    else
        fail "MPV exit=$EXIT"
        info "last 20 log lines:"
        tail -20 "$LOG_MPV" 2>/dev/null | sed 's/^/    /'
    fi
fi
rm -f /tmp/diag-v.bin

# ── 5. Find a live photo ───────────────────────────────────────────────────
section "5. Find a Live Photo"
python3 - <<'PYEOF' 2>&1 | sed 's/^/  /'
import json, os, urllib.request
req = urllib.request.Request(
    f"{os.environ['URL']}/api/search/metadata",
    data=json.dumps({"isMotion": True, "size": 5, "withExif": True}).encode(),
    headers={"x-api-key": os.environ["KEY"], "Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        items = json.load(r).get("assets", {}).get("items", [])
        if not items:
            print("(none found — library has no live photos)")
        for a in items:
            print(f"still={a['id']}  motion={a.get('livePhotoVideoId')}  file={a.get('originalFileName')}")
except Exception as e:
    print(f"error: {e}")
PYEOF

# ── 6. Display / compositor environment ────────────────────────────────────
section "6. Display environment"
info "user systemd env (X/Wayland-related):"
systemctl --user show-environment 2>/dev/null | grep -iE "display|wayland|xdg" | sed 's/^/    /' || true

info "/dev/dri (KMS-capable devices):"
ls -l /dev/dri/ 2>/dev/null | sed 's/^/    /' || info "  none"

info "compositors / display servers running:"
ps -ef | grep -E "Xorg|Xwayland|wayfire|labwc|sway|weston" | grep -v grep | sed 's/^/    /' || info "  none"

info "loginctl session:"
SID=$(loginctl list-sessions --no-legend 2>/dev/null | awk '/seat0/{print $1; exit}')
if [ -n "$SID" ]; then
    loginctl show-session "$SID" 2>/dev/null | grep -iE "^(Type|Class|Active|Service)=" | sed 's/^/    /'
fi

# ── 7. Immframe service inspection ─────────────────────────────────────────
section "7. immframe service"
if systemctl --user is-active immframe >/dev/null 2>&1; then
    ok "immframe.service is active"
    PID=$(systemctl --user show -p MainPID --value immframe 2>/dev/null)
    if [ -n "$PID" ] && [ "$PID" != "0" ]; then
        info "PID: $PID"
        info "open display-related fds:"
        ls -l "/proc/$PID/fd" 2>/dev/null | grep -iE "dri|fb|x11|wayland|/dev/snd" | head -5 | sed 's/^/    /' || info "  (no display fds visible)"
    fi
else
    info "immframe.service is not active — start it with: systemctl --user start immframe"
fi

# ── 8. Force a video slide + capture journal (optional) ───────────────────
section "8. Force a slideshow video (optional)"
IMM=~/immframe/.venv/bin/immframe
if [ ! -x "$IMM" ]; then
    IMM=$(command -v immframe || true)
fi
if [ -z "$IMM" ] || [ ! -x "$IMM" ]; then
    info "immframe CLI not found; skipping"
else
    info "switching mode to random + advancing until a VIDEO comes up..."
    "$IMM" mode random >/dev/null 2>&1 || true
    HIT=0
    for i in 1 2 3 4 5 6 7 8 9 10; do
        sleep 2
        "$IMM" next >/dev/null 2>&1 || true
        KIND=$("$IMM" state 2>/dev/null \
               | python3 -c "import sys,json; print(json.load(sys.stdin).get('current_asset',{}).get('kind',''))" 2>/dev/null)
        printf '  slide %2d: %s\n' "$i" "${KIND:-?}"
        if [ "$KIND" = "VIDEO" ]; then
            HIT=1
            ok "video reached — capturing journal"
            sleep 5
            echo "--- immframe logs (60 lines) ---"
            # immframe may be launched via the user systemd unit OR via
            # labwc autostart piped to systemd-cat (-t immframe). Try both
            # sources and concatenate whatever produces output.
            {
                journalctl --user -u immframe -n 60 --no-pager 2>/dev/null
                journalctl -t immframe -n 60 --no-pager 2>/dev/null
            } | tail -60 | sed 's/^/    /'
            break
        fi
    done
    if [ "$HIT" = "0" ]; then
        info "didn't hit a video in 10 advances — try increasing the loop or run again."
    fi
fi

# ── Done ───────────────────────────────────────────────────────────────────
section "Done"
echo "Full log: $LOG"

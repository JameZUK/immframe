"""`immframe doctor` — check the Immich server and the local kiosk for the
things that quietly degrade a frame, and say what to change.

Every check is a `Finding`: ok / warn / fail plus a one-line remedy.
Nothing here mutates anything: the only "write" is a permission probe
against a nil asset id that Immich rejects before touching data.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .immich.client import STRUCTURED_FILTER_SINCE, ImmichClient, ImmichError

NIL_UUID = "00000000-0000-0000-0000-000000000000"
RECOMMENDED_CLIP = "ViT-B-16-SigLIP2__webli"
SMALL_CLIP_MODELS = {"ViT-B-32__openai", "ViT-B-32__laion2b_e16", "ViT-B-32__laion400m_e32"}


@dataclass
class Finding:
    status: str                      # "ok" | "warn" | "fail" | "info"
    title: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def ok(self, title: str, detail: str = "") -> None:
        self.findings.append(Finding("ok", title, detail))

    def warn(self, title: str, detail: str = "", fix: str = "") -> None:
        self.findings.append(Finding("warn", title, detail, fix))

    def fail(self, title: str, detail: str = "", fix: str = "") -> None:
        self.findings.append(Finding("fail", title, detail, fix))

    def info(self, title: str, detail: str = "") -> None:
        self.findings.append(Finding("info", title, detail))

    @property
    def failed(self) -> bool:
        return any(f.status == "fail" for f in self.findings)


_MARK = {"ok": "✓", "warn": "!", "fail": "✗", "info": "·"}


def render(report: Report, out=sys.stdout) -> None:
    for f in report.findings:
        line = f"{_MARK[f.status]} {f.title}"
        if f.detail:
            line += f" — {f.detail}"
        print(line, file=out)
        if f.fix and f.status in ("warn", "fail"):
            print(f"    → {f.fix}", file=out)
    n_fail = sum(1 for f in report.findings if f.status == "fail")
    n_warn = sum(1 for f in report.findings if f.status == "warn")
    print(file=out)
    print(f"{n_fail} problem(s), {n_warn} recommendation(s)", file=out)


# ── Immich ──────────────────────────────────────────────────────────────


def check_immich(config: Config, client: ImmichClient, rep: Report) -> None:
    rep.info("Immich", config.immich.url)
    if not client.ping():
        rep.fail("Immich reachable", "ping failed",
                 "check immich.url / network / api_key")
        return
    v = client.server_version()
    if v is None:
        rep.warn("Immich version", "could not read /server/version",
                 "flat search filters will be used (deprecated on 3.2+)")
    else:
        dialect = "structured filter" if v >= STRUCTURED_FILTER_SINCE else "flat fields (deprecated on 3.2+)"
        rep.ok("Immich version", f"v{v[0]}.{v[1]}.{v[2]} — search dialect: {dialect}")

    # Permissions — probe the endpoints each feature needs.
    _probe(rep, "asset.read (random / search)", lambda: client.random_assets(1),
           fix="the API key needs asset.read — nothing works without it", fatal=True)
    _probe(rep, "person.read (people mode)", lambda: client.list_people(size=1))
    _probe(rep, "memory.read (memory mode)", lambda: client.list_memories())
    _probe(rep, "search/cities (scene mode labels)", lambda: client.list_cities())
    _probe(rep, "search/statistics (people_min_photos)",
           lambda: client.search_statistics(favorites=True))
    _probe_write(client, rep)

    # Features
    try:
        feats = client._get("/server/features")
    except ImmichError as e:
        feats = {}
        rep.warn("server features", f"unreadable: {e}")
    if isinstance(feats, dict):
        for key, label, fix in (
            ("smartSearch", "smart search (CLIP) enabled",
             "enable Machine Learning → Smart Search in Immich for smart / curated scene modes"),
            ("facialRecognition", "facial recognition enabled",
             "enable it for people mode"),
            ("reverseGeocoding", "reverse geocoding enabled",
             "enable it for location overlays and city scenes"),
        ):
            if feats.get(key):
                rep.ok(label)
            else:
                rep.warn(label, "off", fix)

    _check_system_config(client, rep)
    _check_library(client, rep)


def _probe(rep: Report, label: str, fn: Callable[[], Any], *, fix: str = "", fatal: bool = False) -> bool:
    try:
        fn()
    except ImmichError as e:
        msg = str(e)
        if "permission" in msg.lower() or " 403" in msg:
            (rep.fail if fatal else rep.warn)(label, "key lacks permission", fix or f"grant it in Immich → API Keys ({msg[:80]})")
        else:
            rep.warn(label, msg[:120])
        return False
    rep.ok(label)
    return True


def _probe_write(client: ImmichClient, rep: Report) -> None:
    """Does the (write) key have asset.update? Immich's permission guard
    runs before the asset lookup, so a nil id answers without touching
    anything: 403 = no permission, anything else = permission present."""
    try:
        client.update_asset(NIL_UUID, favorite=True)
        rep.ok("asset.update (dashboard favourite / hide)")
    except ImmichError as e:
        msg = str(e)
        if "permission" in msg.lower() or " 403" in msg:
            rep.warn("asset.update (dashboard favourite / hide)", "key lacks permission",
                     "create a key with asset.update and set immich.write_api_key "
                     "(hide still works locally; favourite / archive-in-Immich won't)")
        else:
            rep.ok("asset.update (dashboard favourite / hide)", "permission present")
    try:
        client.get_edits(NIL_UUID)
        rep.ok("asset.edit.get (dashboard rotate)")
    except ImmichError as e:
        msg = str(e)
        if "permission" in msg.lower() or " 403" in msg:
            rep.warn("asset.edit.* (dashboard rotate)", "key lacks permission",
                     "add asset.edit.get + asset.edit.create (+ asset.edit.delete) to the write key")
        else:
            rep.ok("asset.edit.get (dashboard rotate)", "permission present")


def _check_system_config(client: ImmichClient, rep: Report) -> None:
    try:
        cfg = client._get("/system-config")
    except ImmichError as e:
        rep.info("Immich server settings", f"not readable with this key ({str(e)[:60]}) — "
                 "grant systemConfig.read to have doctor check them")
        return
    if not isinstance(cfg, dict):
        return

    img = cfg.get("image") or {}
    fullsize = (img.get("fullsize") or {}).get("enabled")
    preview_size = (img.get("preview") or {}).get("size")
    if fullsize:
        rep.ok("Immich full-size previews", "enabled — HEIC/RAW photos arrive at original resolution")
    else:
        rep.warn("Immich full-size previews", f"disabled — HEIC/RAW photos arrive as the {preview_size}px preview "
                 "(upscaled on a 4K display)",
                 "Immich → Administration → Settings → Image → enable Full-size previews, "
                 "then Jobs → Generate Thumbnails → Missing")

    ff = cfg.get("ffmpeg") or {}
    policy, target = ff.get("transcode"), str(ff.get("targetResolution", ""))
    if policy in ("optimal", "all", "bitrate") and target in ("720", "1080"):
        rep.ok("Immich video transcoding", f"policy={policy}, target={target}p")
    elif policy == "disabled":
        rep.warn("Immich video transcoding", "disabled — 4K / HEVC originals are streamed as-is",
                 "set policy to 'optimal' with a 1080p target so the Pi only ever decodes ≤1080p H.264")
    else:
        rep.warn("Immich video transcoding", f"policy={policy}, target={target}p — resolution is not "
                 "considered, so 4K H.264 originals are served raw (the Pi 4 can only hardware-decode ≤1080p)",
                 "Immich → Settings → Video Transcoding → policy 'not in accepted format or above target "
                 "resolution' (optimal), target 1080p; then Jobs → Transcode Videos → Missing")

    ml = cfg.get("machineLearning") or {}
    clip = (ml.get("clip") or {}).get("modelName")
    if clip in SMALL_CLIP_MODELS:
        rep.warn("Immich CLIP model", f"{clip} (the small default)",
                 f"Machine Learning → CLIP model → {RECOMMENDED_CLIP} for much better smart / scene "
                 "results, then Jobs → Smart Search → All")
    elif clip:
        rep.ok("Immich CLIP model", clip)

    nightly = cfg.get("nightlyTasks") or {}
    if nightly.get("generateMemories", True):
        rep.ok("Immich memories generated nightly")
    else:
        rep.warn("Immich memories", "nightly generation off", "enable Nightly Tasks → Generate memories for memory mode")


def _check_library(client: ImmichClient, rep: Report) -> None:
    def count(**kw: Any) -> int | None:
        try:
            return client.search_statistics(**kw)
        except ImmichError:
            return None

    total = count()
    if total is not None:
        images = count(with_video=False)
        vids = f", {total - images:,} videos" if images is not None else ""
        rep.info("library", f"{total:,} timeline assets{vids}")
    favs = count(favorites=True)
    if favs is not None:
        if favs == 0:
            rep.warn("favourites", "none starred", "heart photos in Immich (or from the dashboard) to feed favorites mode")
        else:
            rep.ok("favourites", f"{favs:,} starred")
    try:
        people = client.list_people()
        named = [p for p in people if p.get("name") and not p.get("isHidden")]
        starred = sum(1 for p in named if p.get("isFavorite"))
        rep.info("people", f"{len(named)} named of {len(people)} face clusters, {starred} starred")
        if len(named) < 3:
            rep.warn("people mode", "fewer than 3 named people", "name faces in Immich → People")
    except ImmichError:
        pass
    try:
        cities = client.list_cities()
        rep.info("cities", f"{len(cities)} (scene mode labels)")
    except ImmichError:
        pass
    try:
        explore = client.explore()
        if explore.get("things"):
            rep.ok("explore 'things' facet", f"{len(explore['things'])} labels")
        else:
            rep.info("explore 'things' facet", "absent on this Immich — scene mode uses cities; "
                     "set scene_source: curated for themed CLIP scenes")
    except ImmichError:
        pass
    try:
        mems = client.list_memories()
        rep.ok("memories", f"{len(mems)} available") if mems else rep.warn(
            "memories", "none returned", "memory mode will skip until Immich generates some (nightly)")
    except ImmichError:
        pass


# ── Local environment ───────────────────────────────────────────────────


def check_local(config: Config, rep: Report, *, config_path: Path | None = None) -> None:
    rep.info("local", f"python {sys.version.split()[0]} on {sys.platform}")

    # Config file permissions
    if config_path is not None and config_path.exists():
        mode = stat.S_IMODE(config_path.stat().st_mode)
        if mode & 0o077:
            rep.warn("config file permissions", f"{config_path} is {oct(mode)}; it holds API keys",
                     f"chmod 600 {config_path}")
        else:
            rep.ok("config file permissions", oct(mode))

    # Display session
    if os.environ.get("WAYLAND_DISPLAY"):
        rep.ok("display session", f"Wayland ({os.environ['WAYLAND_DISPLAY']})")
    elif os.environ.get("DISPLAY"):
        rep.ok("display session", f"X11 ({os.environ['DISPLAY']})")
    else:
        rep.warn("display session", "no WAYLAND_DISPLAY / DISPLAY in this shell",
                 "run doctor from the frame's session (or over ssh with WAYLAND_DISPLAY=wayland-0) to check the compositor bits")

    # Display power method
    dp = int(config.viewer.raw.get("display_power", 2))
    if dp == 2:
        if shutil.which("wlr-randr"):
            rep.ok("display_power", "2 (wlr-randr present)")
        else:
            rep.fail("display_power", "2 (wlr-randr) but wlr-randr is not installed — the Display switch does nothing",
                     "sudo apt install wlr-randr")
    elif dp == 0:
        rep.warn("display_power", "0 (vcgencmd) — a silent no-op on the KMS driver current Pi OS uses",
                 "set viewer.display_power: 2 (wlr-randr) under labwc")
    else:
        rep.info("display_power", str(dp))

    # Video: libmpv + hardware decoders
    if config.video.enabled:
        try:
            import mpv  # noqa: F401
            rep.ok("python-mpv / libmpv importable")
        except Exception as e:
            rep.fail("python-mpv / libmpv", str(e)[:100], "sudo apt install libmpv2 && pip install python-mpv")
        mpv_bin = shutil.which("mpv")
        if mpv_bin:
            try:
                out = subprocess.run([mpv_bin, "--hwdec=help"], capture_output=True, text=True, timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                out = ""
            hw = [name for name in ("v4l2m2m-copy", "drm-copy", "vaapi", "nvdec", "videotoolbox") if name in out]
            if hw:
                rep.ok("hardware video decoders", ", ".join(hw) + f" (video.hwdec={config.video.hwdec})")
                if config.video.hwdec in ("no", "auto-safe"):
                    rep.warn("video.hwdec", f"{config.video.hwdec} skips the Pi's v4l2m2m decoder",
                             "set video.hwdec: auto-copy")
            else:
                rep.warn("hardware video decoders", "none reported by mpv --hwdec=help",
                         "video will be software-decoded; keep clips ≤1080p via Immich transcoding")
        else:
            rep.info("mpv CLI", "not installed — hardware decoder check skipped (libmpv may still have them)")

    # Cache location
    cache = config.selection.cache_dir or "/dev/shm"
    if Path(cache).is_dir() and os.access(cache, os.W_OK):
        rep.ok("prefetch cache", f"{cache} ({'RAM' if cache.startswith('/dev/shm') else 'disk'})")
    else:
        rep.warn("prefetch cache", f"{cache} unusable — falling back to the system temp dir (SD card on a Pi)",
                 "set selection.cache_dir to a tmpfs path")

    # labwc kiosk files
    home = Path("~").expanduser()
    rc = home / ".config" / "labwc" / "rc.xml"
    auto = home / ".config" / "labwc" / "autostart"
    if rc.exists():
        txt = rc.read_text(errors="replace")
        if "ToggleFullscreen" in txt:
            rep.ok("labwc rc.xml", "ToggleFullscreen rule present")
        else:
            rep.warn("labwc rc.xml", "no ToggleFullscreen window rule", "copy examples/labwc/rc.xml")
    if auto.exists():
        txt = auto.read_text(errors="replace")
        if "immframe" not in txt:
            rep.warn("labwc autostart", "does not launch immframe", "copy examples/labwc/autostart")
        elif "while" in txt:
            rep.ok("labwc autostart", "launches immframe under a restart loop")
        else:
            rep.warn("labwc autostart", "launches immframe without a restart loop — a crash leaves a blank screen until reboot",
                     "copy examples/labwc/autostart (restart loop)")

    # Pi health
    vc = shutil.which("vcgencmd")
    if vc:
        try:
            t = subprocess.run([vc, "get_throttled"], capture_output=True, text=True, timeout=5).stdout.strip()
            val = int(t.split("=")[1], 16) if "=" in t else 0
            if val & 0x50005:
                rep.warn("Pi power / thermal", f"{t} — under-voltage or throttling seen since boot",
                         "use a proper 5 V/3 A supply, check cooling")
            else:
                rep.ok("Pi power / thermal", t)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass


def run(config: Config, *, config_path: Path | None = None, out=sys.stdout) -> int:
    rep = Report()
    client = ImmichClient(
        config.immich.url, config.immich.api_key, timeout_s=15.0,
        write_api_key=config.immich.write_api_key or None,
    )
    try:
        check_immich(config, client, rep)
    finally:
        client.close()
    check_local(config, rep, config_path=config_path)
    render(rep, out)
    return 1 if rep.failed else 0

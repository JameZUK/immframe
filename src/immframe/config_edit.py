"""Read / validate / write the user's config.yaml for the dashboard's
Settings page.

The page works on the *user* YAML (what's in the file), not the merged
result, so it only ever writes keys the user set. A candidate config is
validated by building it exactly the way the daemon does
(`Config.from_user_dict`) before anything touches disk; the write is
atomic and the previous file is kept as `config.yaml.bak`.

Secrets (`api_key`, `write_api_key`, `password`) never leave the server
in clear: they're replaced by `MASK` on the way out, and a value that
comes back still equal to `MASK` is restored from the current file.

`SCHEMA` describes the form the page renders — sections of typed fields
addressed by dotted path — plus the per-mode options a playlist entry
can carry. Anything not in the schema is still editable via the raw
YAML tab.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from .config import SCENE_SOURCES, SELECTION_MODES, Config

MASK = "••••••••"
SECRET_KEYS = frozenset({"api_key", "write_api_key", "password"})

HEADER = """\
# immframe configuration — edited from the web dashboard (Settings).
# Comments are not preserved across saves; the previous file is kept as
# config.yaml.bak. Every key is documented in docs/configuration.md.
"""

_SHOW_TEXT = ["caption", "name", "date", "location", "people", "tags", "ocr"]

# Form schema. type: bool | int | float | str | secret | enum | list
# (list = comma-separated strings). `help` is shown under the field.
SCHEMA: list[dict[str, Any]] = [
    {"section": "immich", "title": "Immich", "fields": [
        {"path": "immich.url", "label": "Server URL", "type": "str", "help": "No trailing slash; /api is appended."},
        {"path": "immich.api_key", "label": "API key", "type": "secret", "help": "Read scope is enough for the slideshow."},
        {"path": "immich.write_api_key", "label": "Write API key", "type": "secret",
         "help": "Optional key for ♥ / Never show again / Rotate: asset.update + asset.edit.get/create/delete. Empty = use the API key."},
        {"path": "immich.image_size", "label": "Image size", "type": "enum", "options": ["fullsize", "preview"],
         "help": "fullsize for 4K displays (needs Immich full-size previews for HEIC/RAW)."},
        {"path": "immich.timeout_s", "label": "Request timeout (s)", "type": "float"},
    ]},
    {"section": "selection", "title": "Selection", "fields": [
        {"path": "selection.default_mode", "label": "Mode", "type": "enum", "options": list(SELECTION_MODES),
         "help": "playlist uses the entries below."},
        {"path": "selection.people_min_photos", "label": "People: min photos", "type": "int",
         "help": "Auto-rotation only picks people with at least this many photos."},
        {"path": "selection.people_favorites_only", "label": "People: starred only", "type": "bool"},
        {"path": "selection.scene_source", "label": "Scene source", "type": "enum", "options": list(SCENE_SOURCES),
         "help": "auto → cities on current Immich; curated = themed CLIP scenes."},
        {"path": "selection.smart_pages", "label": "CLIP pages sampled", "type": "int"},
        {"path": "selection.min_rating", "label": "Random: min rating", "type": "int", "help": "1–5, empty = off."},
        {"path": "selection.recent_days", "label": "Recent: days", "type": "int"},
        {"path": "selection.recent_field", "label": "Recent: by", "type": "enum", "options": ["created", "taken"]},
        {"path": "selection.album_ids", "label": "Album IDs", "type": "list"},
        {"path": "selection.smart_query", "label": "Smart query", "type": "str"},
        {"path": "selection.people_ids", "label": "People IDs", "type": "list", "help": "Empty = rotate everyone named."},
        {"path": "selection.prefetch_count", "label": "Prefetch depth", "type": "int"},
    ]},
    {"section": "viewer", "title": "Display", "fields": [
        {"path": "viewer.time_delay", "label": "Seconds per slide", "type": "float"},
        {"path": "viewer.fade_time", "label": "Crossfade (s)", "type": "float"},
        {"path": "viewer.brightness", "label": "Brightness", "type": "float", "help": "0.0–1.0"},
        {"path": "viewer.show_text", "label": "Overlay fields", "type": "list", "options": _SHOW_TEXT,
         "help": "Space- or comma-separated: " + " ".join(_SHOW_TEXT)},
        {"path": "viewer.show_text_sz", "label": "Overlay text size", "type": "int"},
        {"path": "viewer.show_text_tm", "label": "Overlay shown for (s)", "type": "float"},
        {"path": "viewer.text_bkg_hgt", "label": "Overlay strip height", "type": "float", "help": "Fraction of screen, e.g. 0.25"},
        {"path": "viewer.show_clock", "label": "Clock", "type": "bool"},
        {"path": "viewer.portrait_pairs", "label": "Pair portraits", "type": "bool"},
        {"path": "viewer.mat_images", "label": "Mat images", "type": "bool"},
        {"path": "viewer.blur_edges", "label": "Blur edges", "type": "bool"},
        {"path": "viewer.kenburns", "label": "Ken Burns", "type": "bool"},
        {"path": "viewer.display_power", "label": "Display power method", "type": "enum", "options": [0, 1, 2, 3],
         "help": "2 = wlr-randr (labwc), 1 = xset, 0 = vcgencmd (legacy), 3 = drm sysfs"},
        {"path": "viewer.display_hdmi", "label": "HDMI output", "type": "str"},
    ]},
    {"section": "video", "title": "Video", "fields": [
        {"path": "video.enabled", "label": "Play videos", "type": "bool"},
        {"path": "video.mute", "label": "Mute", "type": "bool"},
        {"path": "video.fit", "label": "Fit", "type": "enum", "options": ["contain", "cover"]},
        {"path": "video.poster", "label": "Poster before video", "type": "bool"},
        {"path": "video.poster_hold_s", "label": "Poster hold (s)", "type": "float"},
        {"path": "video.live_photo_hold_s", "label": "Live photo hold (s)", "type": "float"},
        {"path": "video.max_play_s", "label": "Max clip length (s)", "type": "float"},
        {"path": "video.hwdec", "label": "Hardware decode", "type": "str", "help": "auto-copy (Pi), no = software"},
        {"path": "video.rotate", "label": "Rotate", "type": "enum", "options": ["auto", "no", "0", "90", "180", "270"]},
        {"path": "video.ensure_fullscreen", "label": "Re-assert fullscreen", "type": "bool"},
    ]},
    {"section": "collage", "title": "Collage", "fields": [
        {"path": "collage.enabled", "label": "Collage everything", "type": "bool",
         "help": "Off = only playlist entries marked collage."},
        {"path": "collage.layout", "label": "Layout", "type": "enum", "options": ["auto", "grid", "golden_ratio"]},
        {"path": "collage.min_tiles", "label": "Min tiles", "type": "int"},
        {"path": "collage.max_tiles", "label": "Max tiles", "type": "int"},
        {"path": "collage.smart_caption", "label": "Smart caption", "type": "bool"},
        {"path": "collage.tile_text", "label": "Per-tile caption fields", "type": "str"},
        {"path": "collage.gap", "label": "Gap (px)", "type": "int"},
        {"path": "collage.background", "label": "Background", "type": "str", "help": "hex, e.g. #101018"},
    ]},
    {"section": "control", "title": "Control", "fields": [
        {"path": "control.http.enabled", "label": "HTTP dashboard", "type": "bool"},
        {"path": "control.http.bind", "label": "HTTP bind", "type": "str", "help": "0.0.0.0 for the LAN"},
        {"path": "control.http.port", "label": "HTTP port", "type": "int"},
        {"path": "control.http.auth", "label": "HTTP auth", "type": "bool"},
        {"path": "control.http.username", "label": "HTTP username", "type": "str"},
        {"path": "control.http.password", "label": "HTTP password", "type": "secret"},
        {"path": "control.mqtt.enabled", "label": "MQTT (Home Assistant)", "type": "bool"},
        {"path": "control.mqtt.host", "label": "MQTT host", "type": "str"},
        {"path": "control.mqtt.port", "label": "MQTT port", "type": "int"},
        {"path": "control.mqtt.user", "label": "MQTT user", "type": "str"},
        {"path": "control.mqtt.password", "label": "MQTT password", "type": "secret"},
        {"path": "control.mqtt.base_topic", "label": "MQTT base topic", "type": "str"},
    ]},
]

# Options a playlist entry may carry besides mode / count / collage,
# keyed by mode. Rendered by the playlist builder per row.
PLAYLIST_ENTRY_OPTIONS: dict[str, list[dict[str, Any]]] = {
    "random": [
        {"key": "favorites", "label": "favourites only", "type": "bool"},
        {"key": "min_rating", "label": "min rating", "type": "int"},
        {"key": "album_ids", "label": "album IDs", "type": "list"},
        {"key": "tag_ids", "label": "tag IDs", "type": "list"},
    ],
    "favorites": [
        {"key": "min_rating", "label": "min rating", "type": "int"},
    ],
    "album": [{"key": "album_ids", "label": "album IDs", "type": "list"}],
    "smart": [
        {"key": "smart_query", "label": "query", "type": "str"},
        {"key": "pages", "label": "pages", "type": "int"},
    ],
    "scene": [
        {"key": "source", "label": "source", "type": "enum", "options": list(SCENE_SOURCES)},
        {"key": "pages", "label": "pages", "type": "int"},
    ],
    "people": [
        {"key": "people_ids", "label": "people IDs", "type": "list"},
        {"key": "min_photos", "label": "min photos", "type": "int"},
        {"key": "favorites_only", "label": "starred only", "type": "bool"},
    ],
    "memory": [],
    "recent": [
        {"key": "days", "label": "days", "type": "int"},
        {"key": "field", "label": "by", "type": "enum", "options": ["created", "taken"]},
    ],
}
COLLAGE_ENTRY_OPTIONS: list[dict[str, Any]] = [
    {"key": "layout", "label": "layout", "type": "enum", "options": ["auto", "grid", "golden_ratio"]},
    {"key": "tiles", "label": "tiles", "type": "int"},
    {"key": "min_tiles", "label": "min tiles", "type": "int"},
    {"key": "max_tiles", "label": "max tiles", "type": "int"},
    {"key": "tile_text", "label": "tile text", "type": "str"},
    {"key": "smart_caption", "label": "smart caption", "type": "bool"},
]


class ConfigEditError(ValueError):
    """A candidate config didn't validate. `.message` is user-facing."""


def effective_values(cfg: Config) -> dict[str, Any]:
    """{dotted path: value} for every SCHEMA field, as the daemon would
    actually run it — the user's file merged over the packaged defaults.
    The form renders these (a checkbox for a key the user never set must
    show the *default*, not "off") and writes them back explicitly."""
    from .controller import _VIEWER_DEFAULTS      # lazy: controller pulls in more
    viewer = {**_VIEWER_DEFAULTS, **cfg.viewer.raw}
    out: dict[str, Any] = {}
    for section in SCHEMA:
        for f in section["fields"]:
            path = f["path"]
            parts = path.split(".")
            if parts[0] == "viewer":
                val = viewer.get(parts[1])
            else:
                obj: Any = cfg
                for part in parts:
                    obj = getattr(obj, part, None)
                    if obj is None:
                        break
                val = obj
            if parts[-1] in SECRET_KEYS and val:
                val = MASK
            if isinstance(val, Path):
                val = str(val)
            out[path] = val
    return out


def read_user_yaml(path: Path | None) -> dict[str, Any]:
    """The user's YAML as a dict ({} when there is no file yet)."""
    if path is None or not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigEditError(f"{path} is not a mapping at the top level")
    return data


def mask(tree: Any) -> Any:
    """Copy of `tree` with every secret value replaced by MASK."""
    if isinstance(tree, dict):
        return {
            k: (MASK if k in SECRET_KEYS and v else mask(v))
            for k, v in tree.items()
        }
    if isinstance(tree, list):
        return [mask(v) for v in tree]
    return tree


def unmask(new: Any, current: Any) -> Any:
    """Restore secrets the client sent back untouched (still == MASK) from
    the current on-disk tree, walking both in parallel."""
    if isinstance(new, dict):
        cur = current if isinstance(current, dict) else {}
        out = {}
        for k, v in new.items():
            if k in SECRET_KEYS and v == MASK:
                out[k] = cur.get(k, "")
            else:
                out[k] = unmask(v, cur.get(k))
        return out
    if isinstance(new, list):
        cur = current if isinstance(current, list) else []
        return [unmask(v, cur[i] if i < len(cur) else None) for i, v in enumerate(new)]
    return new


def to_yaml(tree: dict[str, Any]) -> str:
    return HEADER + yaml.safe_dump(
        tree, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100,
    )


def parse_yaml(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigEditError(f"YAML error: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigEditError("config must be a mapping at the top level")
    return data


def validate(tree: dict[str, Any]) -> Config:
    """Build a Config from the candidate exactly as the daemon would;
    raises ConfigEditError with the loader's message on failure."""
    try:
        return Config.from_user_dict(tree)
    except (ValueError, KeyError, TypeError) as e:
        raise ConfigEditError(str(e)) from e


def save(path: Path, tree: dict[str, Any]) -> Path:
    """Write `tree` as YAML to `path` atomically (0600), keeping the
    previous file as `<name>.bak`. Returns the backup path (or `path`
    when there was nothing to back up)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bak")
    if path.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(to_yaml(tree))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return backup if backup.exists() else path


def prune(tree: Any) -> Any:
    """Drop None and empty containers so the saved YAML only carries values
    the user actually set (the form sends null for blank numbers and omits
    blank text). Empty strings are kept: `show_text: ""` means "no overlay".
    """
    if isinstance(tree, dict):
        out = {}
        for k, v in tree.items():
            pv = prune(v)
            if pv is None or (isinstance(pv, (dict, list)) and not pv):
                continue
            out[k] = pv
        return out
    if isinstance(tree, list):
        return [prune(v) for v in tree if v is not None]
    return tree

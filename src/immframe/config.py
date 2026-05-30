"""Config loading and validation.

Search order for the user's YAML:
    1. `--config` CLI arg (passed in as `path` here)
    2. `$IMMFRAME_CONFIG` env var
    3. `~/.config/immframe/config.yaml`
    4. `/etc/immframe/config.yaml`

The package's `_defaults/default.yaml` is deep-merged underneath so optional
keys keep their defaults. Unknown top-level sections raise — typos should
fail loudly.

Secrets (Immich API key, MQTT password, HTTP auth password) live inline in
the YAML. The config file MUST be `chmod 600`. For setups that prefer to
keep secrets out of the file, any string value supports `${ENV_VAR}`
substitution from the process environment.

    immich:
      api_key: ${IMMICH_API_KEY}
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml


SelectionMode = Literal["random", "album", "smart", "scene", "people", "memory", "recent", "playlist"]

_DEFAULT_YAML_PATH = Path(__file__).parent / "_defaults" / "default.yaml"
_SEARCH_PATHS = (
    Path("~/.config/immframe/config.yaml").expanduser(),
    Path("/etc/immframe/config.yaml"),
)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_path(p: str | Path) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser()


def _expand_str(s: str) -> str:
    """Substitute ${VAR} from the environment. Missing vars become ''."""
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), s)


def _expand_tree(value: Any) -> Any:
    """Recursively walk dict/list and expand ${VAR} in every string leaf."""
    if isinstance(value, str):
        return _expand_str(value)
    if isinstance(value, dict):
        return {k: _expand_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_tree(v) for v in value]
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` on top of `base`. Lists replace, not concatenate."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class ImmichConfig:
    url: str
    api_key: str
    timeout_s: float = 10.0
    image_size: str = "fullsize"               # 'preview' (~1440px) or 'fullsize' (original)


@dataclass
class SelectionConfig:
    default_mode: SelectionMode = "random"
    album_ids: list[str] = field(default_factory=list)
    smart_query: str = ""
    people_ids: list[str] = field(default_factory=list)  # empty = rotate all named people
    recent_days: int = 30                                # "recent" mode window in days
    recent_field: str = "created"                        # 'created' (uploaded) or 'taken'
    # playlist mode: list of entry dicts, each with at least `mode` and `count`.
    # See docs/configuration.md for the schema.
    playlist: list[dict[str, Any]] = field(default_factory=list)
    prefetch_count: int = 5


@dataclass
class VideoConfig:
    enabled: bool = True
    stream: bool = True
    mute: bool = True
    # MPV video output. Sensible defaults per environment:
    #   "gpu"   — default; KMS/DRM on Pi, X11 GL elsewhere
    #   "x11"   — pure X11 (force xwindow context)
    #   "drm"   — direct framebuffer (KMS, no X server)
    #   "sdl"   — SDL2 (works inside pi3d's window in some setups)
    vo: str = "gpu"
    # How MPV fits the video into the window:
    #   "contain" — preserve aspect, letterbox/pillarbox as needed (default)
    #   "cover"   — preserve aspect, fill the screen, crop the overflow
    fit: str = "contain"
    # How long to hold the still before triggering live-photo motion clip
    live_photo_hold_s: float = 1.0
    # Show the (matted) preview JPEG of a video before playing it. When
    # true: video assets render through pi3d first like images, giving
    # them the mat / blur-edges / fade-in treatment, then MPV takes over
    # for playback. When false: videos go straight to MPV fullscreen.
    poster: bool = True
    # Seconds to hold the matted poster before MPV starts. Independent of
    # live_photo_hold_s since videos are longer-form content and benefit
    # from a longer pre-roll.
    poster_hold_s: float = 3.0
    # MPV --video-rotate option:
    #   "auto"  — honor rotation tag in container metadata (default)
    #   "0"/"90"/"180"/"270" — force a specific clockwise rotation
    #   "no"    — disable rotation entirely
    rotate: str = "auto"
    # Whether MPV requests its own fullscreen. Default false: on the labwc
    # kiosk the compositor fullscreens every window, and a second fullscreen
    # request from MPV gets toggled back to a tiny window. Set true only when
    # running under a compositor that does NOT auto-fullscreen (plain desktop).
    fullscreen: bool = False
    # Hard cap for any single video play (seconds) — slideshow advances
    # even if MPV hasn't reported EOF (bad codec, network stall, ...)
    max_play_s: float = 60.0


@dataclass
class ViewerConfig:
    """Carries the viewer-block YAML through to picframe's vendored viewer
    untouched — see picframe wiki for the full key reference."""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = "homeassistant.local"
    port: int = 1883
    user: str = ""
    password: str = ""
    base_topic: str = "immframe"


@dataclass
class HttpConfig:
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8080
    auth: bool = True
    username: str = ""
    password: str = ""


@dataclass
class ControlConfig:
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    http: HttpConfig = field(default_factory=HttpConfig)


@dataclass
class Config:
    immich: ImmichConfig
    selection: SelectionConfig
    video: VideoConfig
    viewer: ViewerConfig
    control: ControlConfig

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Locate user config, deep-merge with packaged defaults, build dataclasses."""
        user_path = cls._locate(path)
        with _DEFAULT_YAML_PATH.open() as f:
            merged = yaml.safe_load(f) or {}
        if user_path is not None:
            with user_path.open() as f:
                user = yaml.safe_load(f) or {}
            merged = _deep_merge(merged, user)
        merged = _expand_tree(merged)
        return cls._build(merged)

    @staticmethod
    def _locate(explicit: Path | None) -> Path | None:
        if explicit is not None:
            p = _expand_path(explicit)
            if not p.exists():
                raise FileNotFoundError(f"Config not found: {p}")
            return p
        env = os.environ.get("IMMFRAME_CONFIG")
        if env:
            p = _expand_path(env)
            if not p.exists():
                raise FileNotFoundError(f"$IMMFRAME_CONFIG points at missing file: {p}")
            return p
        for candidate in _SEARCH_PATHS:
            if candidate.exists():
                return candidate
        return None

    @classmethod
    def _build(cls, data: dict[str, Any]) -> "Config":
        known = {"immich", "selection", "video", "viewer", "control"}
        unknown = set(data.keys()) - known
        if unknown:
            raise ValueError(f"Unknown top-level config keys: {sorted(unknown)}")

        immich_raw = data["immich"]
        api_key = (immich_raw.get("api_key") or "").strip()
        if not api_key:
            raise ValueError(
                "immich.api_key is empty — set it inline or via ${ENV_VAR} substitution"
            )
        image_size = immich_raw.get("image_size", "fullsize")
        if image_size not in ("preview", "fullsize"):
            raise ValueError(
                f"immich.image_size must be 'preview' or 'fullsize'; got {image_size!r}"
            )
        immich = ImmichConfig(
            url=immich_raw["url"].rstrip("/"),
            api_key=api_key,
            timeout_s=float(immich_raw.get("timeout_s", 10.0)),
            image_size=image_size,
        )

        sel_raw = data.get("selection", {})
        selection = SelectionConfig(
            default_mode=sel_raw.get("default_mode", "random"),
            album_ids=list(sel_raw.get("album_ids", [])),
            smart_query=sel_raw.get("smart_query", ""),
            people_ids=list(sel_raw.get("people_ids", [])),
            recent_days=int(sel_raw.get("recent_days", 30)),
            recent_field=sel_raw.get("recent_field", "created"),
            playlist=list(sel_raw.get("playlist", [])),
            prefetch_count=int(sel_raw.get("prefetch_count", 5)),
        )
        valid_modes = ("random", "album", "smart", "scene", "people", "memory", "recent", "playlist")
        if selection.default_mode not in valid_modes:
            raise ValueError(
                f"selection.default_mode invalid: {selection.default_mode!r}; valid: {valid_modes}"
            )
        if selection.recent_field not in ("created", "taken"):
            raise ValueError(
                f"selection.recent_field must be 'created' or 'taken'; got {selection.recent_field!r}"
            )

        vid_raw = data.get("video", {})
        # YAML 1.1 boolification: unquoted `no`/`yes` parse to False/True.
        # We want `rotate: no` to mean "disable rotation" (string "no"), so
        # convert booleans back to their string equivalents here.
        rotate_raw = vid_raw.get("rotate", "auto")
        if isinstance(rotate_raw, bool):
            rotate_raw = "no" if not rotate_raw else "auto"
        else:
            rotate_raw = str(rotate_raw)
        video = VideoConfig(
            enabled=bool(vid_raw.get("enabled", True)),
            stream=bool(vid_raw.get("stream", True)),
            mute=bool(vid_raw.get("mute", True)),
            vo=vid_raw.get("vo", "gpu"),
            fit=str(vid_raw.get("fit", "contain")),
            live_photo_hold_s=float(vid_raw.get("live_photo_hold_s", 1.0)),
            poster=bool(vid_raw.get("poster", True)),
            poster_hold_s=float(vid_raw.get("poster_hold_s", 3.0)),
            rotate=rotate_raw,
            fullscreen=bool(vid_raw.get("fullscreen", False)),
            max_play_s=float(vid_raw.get("max_play_s", 60.0)),
        )
        # rotate validation
        valid_rot = ("auto", "no", "0", "90", "180", "270")
        if video.rotate not in valid_rot:
            raise ValueError(
                f"video.rotate must be one of {valid_rot}; got {video.rotate!r}"
            )
        if video.fit not in ("contain", "cover"):
            raise ValueError(
                f"video.fit must be 'contain' or 'cover'; got {video.fit!r}"
            )

        viewer = ViewerConfig(raw=dict(data.get("viewer", {})))

        ctrl_raw = data.get("control", {})
        mqtt_raw = ctrl_raw.get("mqtt", {})
        http_raw = ctrl_raw.get("http", {})
        control = ControlConfig(
            mqtt=MqttConfig(
                enabled=bool(mqtt_raw.get("enabled", False)),
                host=mqtt_raw.get("host", "homeassistant.local"),
                port=int(mqtt_raw.get("port", 1883)),
                user=mqtt_raw.get("user", ""),
                password=mqtt_raw.get("password", ""),
                base_topic=mqtt_raw.get("base_topic", "immframe"),
            ),
            http=HttpConfig(
                enabled=bool(http_raw.get("enabled", False)),
                bind=http_raw.get("bind", "127.0.0.1"),
                port=int(http_raw.get("port", 8080)),
                auth=bool(http_raw.get("auth", True)),
                username=http_raw.get("username", ""),
                password=http_raw.get("password", ""),
            ),
        )

        return cls(immich=immich, selection=selection, video=video, viewer=viewer, control=control)

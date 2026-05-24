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


SelectionMode = Literal["random", "album", "smart", "scene", "people"]

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
    prefetch_count: int = 5


@dataclass
class VideoConfig:
    enabled: bool = True
    stream: bool = True
    mute: bool = True


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
            prefetch_count=int(sel_raw.get("prefetch_count", 5)),
        )
        if selection.default_mode not in ("random", "album", "smart", "scene", "people"):
            raise ValueError(f"selection.default_mode invalid: {selection.default_mode!r}")

        vid_raw = data.get("video", {})
        video = VideoConfig(
            enabled=bool(vid_raw.get("enabled", True)),
            stream=bool(vid_raw.get("stream", True)),
            mute=bool(vid_raw.get("mute", True)),
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

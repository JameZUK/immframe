"""Config loading and validation.

Search order for the user's YAML:
    1. `--config` CLI arg (passed in as `path` here)
    2. `$IMMFRAME_CONFIG` env var
    3. `~/.config/immframe/config.yaml`
    4. `/etc/immframe/config.yaml`

The package's `_defaults/default.yaml` is deep-merged underneath so optional
keys keep their defaults. Unknown top-level sections raise — typos in config
should fail loudly, not silently.

API keys are loaded from a separate `api_key_file` — never from the YAML
itself, so the config is safe to commit / share / paste into a bug report.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml


SelectionMode = Literal["random", "album", "smart"]
InputType = Literal["keyboard", "mouse", "touch", "none"]

_DEFAULT_YAML_PATH = Path(__file__).parent / "_defaults" / "default.yaml"
_SEARCH_PATHS = (
    Path("~/.config/immframe/config.yaml").expanduser(),
    Path("/etc/immframe/config.yaml"),
)


def _expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser()


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` on top of `base`. Lists are replaced, not concatenated."""
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
    api_key_file: Path
    timeout_s: float = 10.0

    @property
    def api_key(self) -> str:
        text = self.api_key_file.read_text().strip()
        if not text:
            raise ValueError(f"API key file is empty: {self.api_key_file}")
        return text


@dataclass
class SelectionConfig:
    default_mode: SelectionMode = "random"
    album_ids: list[str] = field(default_factory=list)
    smart_query: str = ""
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
    password_file: Path | None = None
    base_topic: str = "immframe"


@dataclass
class HttpConfig:
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 8080
    auth: bool = True
    auth_file: Path | None = None


@dataclass
class PeripheralsConfig:
    enabled: bool = True
    input_type: InputType = "keyboard"


@dataclass
class ControlConfig:
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    peripherals: PeripheralsConfig = field(default_factory=PeripheralsConfig)


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
        return cls._build(merged)

    @staticmethod
    def _locate(explicit: Path | None) -> Path | None:
        if explicit is not None:
            p = _expand(explicit)
            if not p.exists():
                raise FileNotFoundError(f"Config not found: {p}")
            return p
        env = os.environ.get("IMMFRAME_CONFIG")
        if env:
            p = _expand(env)
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
        immich = ImmichConfig(
            url=immich_raw["url"].rstrip("/"),
            api_key_file=_expand(immich_raw["api_key_file"]),
            timeout_s=float(immich_raw.get("timeout_s", 10.0)),
        )

        sel_raw = data.get("selection", {})
        selection = SelectionConfig(
            default_mode=sel_raw.get("default_mode", "random"),
            album_ids=list(sel_raw.get("album_ids", [])),
            smart_query=sel_raw.get("smart_query", ""),
            prefetch_count=int(sel_raw.get("prefetch_count", 5)),
        )
        if selection.default_mode not in ("random", "album", "smart"):
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
        per_raw = ctrl_raw.get("peripherals", {})
        control = ControlConfig(
            mqtt=MqttConfig(
                enabled=bool(mqtt_raw.get("enabled", False)),
                host=mqtt_raw.get("host", "homeassistant.local"),
                port=int(mqtt_raw.get("port", 1883)),
                user=mqtt_raw.get("user", ""),
                password_file=_expand(mqtt_raw["password_file"]) if mqtt_raw.get("password_file") else None,
                base_topic=mqtt_raw.get("base_topic", "immframe"),
            ),
            http=HttpConfig(
                enabled=bool(http_raw.get("enabled", False)),
                bind=http_raw.get("bind", "127.0.0.1"),
                port=int(http_raw.get("port", 8080)),
                auth=bool(http_raw.get("auth", True)),
                auth_file=_expand(http_raw["auth_file"]) if http_raw.get("auth_file") else None,
            ),
            peripherals=PeripheralsConfig(
                enabled=bool(per_raw.get("enabled", True)),
                input_type=per_raw.get("input_type", "keyboard"),
            ),
        )
        if control.peripherals.input_type not in ("keyboard", "mouse", "touch", "none"):
            raise ValueError(
                f"control.peripherals.input_type invalid: {control.peripherals.input_type!r}"
            )

        return cls(immich=immich, selection=selection, video=video, viewer=viewer, control=control)

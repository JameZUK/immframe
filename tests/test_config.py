from __future__ import annotations

from pathlib import Path

import pytest

from immframe.config import Config


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_load_minimal(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: secret-key
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.immich.url == "https://immich.example"
    assert cfg.immich.api_key == "secret-key"
    assert cfg.selection.default_mode == "random"
    assert cfg.video.enabled is True


def test_url_trailing_slash_stripped(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example/
  api_key: k
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.immich.url == "https://immich.example"


def test_unknown_top_level_raises(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
mystery: yes
""",
    )
    with pytest.raises(ValueError, match="Unknown top-level"):
        Config.load(cfg_yaml)


def test_bad_selection_mode_raises(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
selection:
  default_mode: lottery
""",
    )
    with pytest.raises(ValueError, match="default_mode"):
        Config.load(cfg_yaml)


def test_empty_api_key_raises(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: ""
""",
    )
    with pytest.raises(ValueError, match="api_key.*empty"):
        Config.load(cfg_yaml)


def test_missing_api_key_raises(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
""",
    )
    with pytest.raises(ValueError, match="api_key.*empty"):
        Config.load(cfg_yaml)


def test_env_var_substitution(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MY_IMMICH_KEY", "from-env")
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: ${MY_IMMICH_KEY}
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.immich.api_key == "from-env"


def test_env_var_missing_becomes_empty_and_errors(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MY_IMMICH_KEY", raising=False)
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: ${MY_IMMICH_KEY}
""",
    )
    with pytest.raises(ValueError, match="api_key.*empty"):
        Config.load(cfg_yaml)


def test_env_var_in_mqtt_password(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MQTT_PW", "broker-pw")
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
control:
  mqtt:
    enabled: true
    password: ${MQTT_PW}
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.control.mqtt.password == "broker-pw"
    assert cfg.control.mqtt.enabled is True


def test_missing_config_with_explicit_path_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "nope.yaml")


def test_empty_immich_url_raises(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: ""
  api_key: k
""",
    )
    with pytest.raises(ValueError, match="immich.url"):
        Config.load(cfg_yaml)


def test_prefetch_count_floored_at_one(tmp_path: Path):
    """prefetch_count: 0 must not become an unbounded queue."""
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
selection:
  prefetch_count: 0
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.selection.prefetch_count == 1


def test_http_inline_credentials(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
control:
  http:
    enabled: true
    username: admin
    password: hunter2
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.control.http.username == "admin"
    assert cfg.control.http.password == "hunter2"


def test_video_rotate_valid_values(tmp_path: Path):
    """auto / no / 0 / 90 / 180 / 270 must all be accepted."""
    for v in ("auto", "no", "0", "90", "180", "270"):
        cfg_yaml = _write(
            tmp_path,
            f"config-{v}.yaml",
            f"""
immich:
  url: https://immich.example
  api_key: k
video:
  rotate: {v}
""",
        )
        cfg = Config.load(cfg_yaml)
        assert cfg.video.rotate == v


def test_video_rotate_rejects_bad_value(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
video:
  rotate: 45
""",
    )
    with pytest.raises(ValueError, match="video.rotate"):
        Config.load(cfg_yaml)


def test_video_fit_defaults_contain(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.video.fit == "contain"


def test_video_fit_cover(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
video:
  fit: cover
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.video.fit == "cover"


def test_video_fit_rejects_bad_value(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
video:
  fit: stretch
""",
    )
    with pytest.raises(ValueError, match="video.fit"):
        Config.load(cfg_yaml)


def test_video_fullscreen_defaults_off(tmp_path: Path):
    """Default is off so the compositor (labwc) owns fullscreen — a second
    fullscreen request from MPV gets toggled back to a tiny window."""
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.video.fullscreen is False


def test_video_fullscreen_override(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
video:
  fullscreen: true
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.video.fullscreen is True


def test_collage_defaults_disabled(tmp_path: Path):
    cfg = Config.load(
        _write(tmp_path, "config.yaml", """
immich:
  url: https://immich.example
  api_key: k
""")
    )
    assert cfg.collage.enabled is False
    assert cfg.collage.layout == "auto"
    assert cfg.collage.min_tiles == 3
    assert cfg.collage.max_tiles == 6
    assert cfg.collage.fit == "cover"


def test_collage_enabled_overrides(tmp_path: Path):
    cfg = Config.load(
        _write(tmp_path, "config.yaml", """
immich:
  url: https://immich.example
  api_key: k
collage:
  enabled: true
  layout: golden_ratio
  min_tiles: 4
  max_tiles: 4
  gap: 12
  background: "#223344"
  fit: contain
""")
    )
    assert cfg.collage.enabled is True
    assert cfg.collage.layout == "golden_ratio"
    assert cfg.collage.min_tiles == 4 and cfg.collage.max_tiles == 4
    assert cfg.collage.gap == 12
    assert cfg.collage.background == "#223344"
    assert cfg.collage.fit == "contain"


@pytest.mark.parametrize("block,match", [
    ("layout: spiral", "collage.layout"),
    ("fit: stretch", "collage.fit"),
    ("min_tiles: 1", "collage.min_tiles"),
    ("min_tiles: 5\n  max_tiles: 3", "collage.max_tiles"),
    ("max_tiles: 99", "collage.max_tiles"),
    ("gap: -3", "collage.gap"),
    ('background: "zzz"', "hex color"),
])
def test_collage_invalid_values_raise(tmp_path: Path, block: str, match: str):
    cfg_yaml = _write(tmp_path, "config.yaml", f"""
immich:
  url: https://immich.example
  api_key: k
collage:
  enabled: true
  {block}
""")
    with pytest.raises(ValueError, match=match):
        Config.load(cfg_yaml)


def test_video_poster_defaults(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.video.poster is True
    assert cfg.video.poster_hold_s == 3.0
    assert cfg.video.rotate == "auto"


def test_video_poster_overrides(tmp_path: Path):
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        """
immich:
  url: https://immich.example
  api_key: k
video:
  poster: false
  poster_hold_s: 0.5
  rotate: "180"
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.video.poster is False
    assert cfg.video.poster_hold_s == 0.5
    assert cfg.video.rotate == "180"

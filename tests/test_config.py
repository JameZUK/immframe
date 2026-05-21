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

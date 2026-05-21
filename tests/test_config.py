from __future__ import annotations

from pathlib import Path

import pytest

from immframe.config import Config


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_load_minimal(tmp_path: Path):
    key = _write(tmp_path, "api_key", "secret-key")
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        f"""
immich:
  url: https://immich.example
  api_key_file: {key}
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.immich.url == "https://immich.example"
    assert cfg.immich.api_key == "secret-key"
    assert cfg.selection.default_mode == "random"
    assert cfg.video.enabled is True


def test_url_trailing_slash_stripped(tmp_path: Path):
    key = _write(tmp_path, "api_key", "k")
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        f"""
immich:
  url: https://immich.example/
  api_key_file: {key}
""",
    )
    cfg = Config.load(cfg_yaml)
    assert cfg.immich.url == "https://immich.example"


def test_unknown_top_level_raises(tmp_path: Path):
    key = _write(tmp_path, "api_key", "k")
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        f"""
immich:
  url: https://immich.example
  api_key_file: {key}
mystery: yes
""",
    )
    with pytest.raises(ValueError, match="Unknown top-level"):
        Config.load(cfg_yaml)


def test_bad_selection_mode_raises(tmp_path: Path):
    key = _write(tmp_path, "api_key", "k")
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        f"""
immich:
  url: https://immich.example
  api_key_file: {key}
selection:
  default_mode: lottery
""",
    )
    with pytest.raises(ValueError, match="default_mode"):
        Config.load(cfg_yaml)


def test_empty_api_key_file_raises(tmp_path: Path):
    key = _write(tmp_path, "api_key", "")
    cfg_yaml = _write(
        tmp_path,
        "config.yaml",
        f"""
immich:
  url: https://immich.example
  api_key_file: {key}
""",
    )
    cfg = Config.load(cfg_yaml)
    with pytest.raises(ValueError, match="empty"):
        _ = cfg.immich.api_key


def test_missing_config_with_explicit_path_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "nope.yaml")

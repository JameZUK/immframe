from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from immframe import config_edit as ce


def test_mask_and_unmask_round_trip_secrets():
    tree = {"immich": {"url": "u", "api_key": "SECRET", "write_api_key": ""},
            "control": {"http": {"password": "pw"}, "mqtt": {"password": ""}}}
    masked = ce.mask(tree)
    assert masked["immich"]["api_key"] == ce.MASK
    assert masked["immich"]["write_api_key"] == ""            # empty stays empty (not masked)
    assert masked["control"]["http"]["password"] == ce.MASK
    # Client edits the URL, leaves the mask, sets a new http password.
    edited = ce.mask(tree)
    edited["immich"]["url"] = "https://new"
    edited["control"]["http"]["password"] = "newpw"
    out = ce.unmask(edited, tree)
    assert out["immich"]["api_key"] == "SECRET"
    assert out["immich"]["url"] == "https://new"
    assert out["control"]["http"]["password"] == "newpw"
    assert out["control"]["mqtt"]["password"] == ""


def test_unmask_handles_new_sections_and_lists():
    current = {"selection": {"playlist": [{"mode": "random"}]}}
    new = {"selection": {"playlist": [{"mode": "random"}, {"mode": "people", "people_ids": ["a"]}]},
           "control": {"mqtt": {"password": ce.MASK}}}
    out = ce.unmask(new, current)
    assert out["selection"]["playlist"][1]["people_ids"] == ["a"]
    assert out["control"]["mqtt"]["password"] == ""           # nothing to restore → empty


def test_prune_drops_none_and_empty_containers_but_keeps_empty_strings():
    tree = {"a": None, "b": {}, "c": [], "d": "", "e": {"f": None, "g": 1}, "h": [None, 2]}
    assert ce.prune(tree) == {"d": "", "e": {"g": 1}, "h": [2]}


def test_validate_uses_the_daemon_loader():
    cfg = ce.validate({"immich": {"url": "https://x", "api_key": "k"}, "selection": {"default_mode": "favorites"}})
    assert cfg.selection.default_mode == "favorites"
    with pytest.raises(ce.ConfigEditError, match="default_mode"):
        ce.validate({"immich": {"url": "https://x", "api_key": "k"}, "selection": {"default_mode": "nope"}})
    with pytest.raises(ce.ConfigEditError, match="api_key"):
        ce.validate({"immich": {"url": "https://x"}})
    with pytest.raises(ce.ConfigEditError, match="Unknown top-level"):
        ce.validate({"immich": {"url": "https://x", "api_key": "k"}, "typo": 1})


def test_parse_yaml_errors_are_user_facing():
    assert ce.parse_yaml("") == {}
    with pytest.raises(ce.ConfigEditError, match="YAML error"):
        ce.parse_yaml("immich: [unclosed")
    with pytest.raises(ce.ConfigEditError, match="mapping"):
        ce.parse_yaml("- a\n- b\n")


def test_save_is_atomic_0600_with_backup(tmp_path: Path):
    path = tmp_path / "cfg" / "config.yaml"
    tree = {"immich": {"url": "https://x", "api_key": "k"}}
    backup = ce.save(path, tree)
    assert backup == path                                      # nothing to back up first time
    assert path.stat().st_mode & 0o777 == 0o600
    assert yaml.safe_load(path.read_text()) == tree
    assert path.read_text().startswith("# immframe configuration")
    tree2 = {"immich": {"url": "https://y", "api_key": "k"}}
    backup = ce.save(path, tree2)
    assert backup == path.with_name("config.yaml.bak")
    assert yaml.safe_load(backup.read_text()) == tree
    assert yaml.safe_load(path.read_text()) == tree2
    assert not path.with_name("config.yaml.part").exists()


def test_read_user_yaml_missing_and_bad(tmp_path: Path):
    assert ce.read_user_yaml(None) == {}
    assert ce.read_user_yaml(tmp_path / "nope.yaml") == {}
    bad = tmp_path / "bad.yaml"; bad.write_text("- list\n")
    with pytest.raises(ce.ConfigEditError):
        ce.read_user_yaml(bad)


def test_schema_paths_are_valid_config_keys():
    """Every schema field must survive the loader when set to a sane value
    — catches typos in dotted paths."""
    base = {"immich": {"url": "https://x", "api_key": "k"}}
    for section in ce.SCHEMA:
        for f in section["fields"]:
            tree = yaml.safe_load(yaml.safe_dump(base))
            sample = {"bool": True, "int": 2, "float": 2.0, "str": "x", "secret": "s",
                      "enum": f.get("options", [None])[0], "list": []}[f["type"]]
            if f["path"] == "selection.min_rating":
                sample = 3
            if f["path"] == "viewer.brightness":
                sample = 0.5
            if f["path"] == "immich.url":
                sample = "https://y"
            if f["path"] == "collage.max_tiles":
                sample = 6
            if f["path"] == "collage.background":
                sample = "#101018"
            keys = f["path"].split(".")
            o = tree
            for k in keys[:-1]:
                o = o.setdefault(k, {})
            o[keys[-1]] = sample
            ce.validate(tree)                                   # must not raise



def test_effective_values_merge_defaults_and_mask():
    cfg = ce.validate({"immich": {"url": "https://x", "api_key": "k"}, "viewer": {"time_delay": 45}})
    eff = ce.effective_values(cfg)
    assert eff["video.poster"] is True                       # default, not "unset → off"
    assert eff["viewer.time_delay"] == 45
    assert eff["viewer.portrait_pairs"] is True
    assert eff["viewer.display_power"] == 2
    assert eff["immich.api_key"] == ce.MASK
    assert eff["immich.write_api_key"] == ""                 # empty secret stays empty
    assert eff["selection.min_rating"] is None
    assert set(f["path"] for sct in ce.SCHEMA for f in sct["fields"]) == set(eff)

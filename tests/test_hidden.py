from __future__ import annotations

import json
from pathlib import Path

from immframe.hidden import HiddenList


def test_starts_empty_without_file(tmp_path: Path):
    h = HiddenList(tmp_path / "state" / "hidden.json")
    assert len(h) == 0 and "x" not in h
    assert not (tmp_path / "state").exists()               # nothing written yet


def test_add_persists_atomically_and_reloads(tmp_path: Path):
    path = tmp_path / "state" / "hidden.json"
    h = HiddenList(path)
    assert h.add("a", "b") is True
    assert h.add("a") is False                              # no change, no rewrite
    assert "a" in h and "b" in h and len(h) == 2
    assert json.loads(path.read_text()) == {"hidden": ["a", "b"]}
    assert not path.with_name("hidden.json.part").exists()
    again = HiddenList(path)
    assert "a" in again and "b" in again


def test_corrupt_file_starts_empty(tmp_path: Path):
    path = tmp_path / "hidden.json"
    path.write_text("{not json")
    h = HiddenList(path)
    assert len(h) == 0
    h.add("z")
    assert json.loads(path.read_text()) == {"hidden": ["z"]}


def test_ignores_non_string_entries(tmp_path: Path):
    path = tmp_path / "hidden.json"
    path.write_text(json.dumps({"hidden": ["ok", 5, None]}))
    h = HiddenList(path)
    assert len(h) == 1 and "ok" in h

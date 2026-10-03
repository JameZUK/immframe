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
    assert "a" in h and "b" in h and len(h) == 1         # "b" is a's companion
    saved = json.loads(path.read_text())
    assert saved["hidden"] == ["a", "b"]
    assert saved["details"]["a"]["companions"] == ["b"]
    assert not path.with_name("hidden.json.part").exists()
    again = HiddenList(path)
    assert "a" in again and "b" in again


def test_corrupt_file_starts_empty(tmp_path: Path):
    path = tmp_path / "hidden.json"
    path.write_text("{not json")
    h = HiddenList(path)
    assert len(h) == 0
    h.add("z")
    assert json.loads(path.read_text())["hidden"] == ["z"]


def test_ignores_non_string_entries(tmp_path: Path):
    path = tmp_path / "hidden.json"
    path.write_text(json.dumps({"hidden": ["ok", 5, None]}))
    h = HiddenList(path)
    assert len(h) == 1 and "ok" in h



def test_details_entries_newest_first_and_legacy_last(tmp_path: Path):
    path = tmp_path / "hidden.json"
    path.write_text(json.dumps({"hidden": ["legacy"]}))           # old format, id only
    h = HiddenList(path)
    h.add("old", details={"file": "old.jpg", "kind": "IMAGE", "at": 100})
    h.add("new", "clip", details={"file": "new.heic", "kind": "IMAGE", "at": 200})
    ents = h.entries()
    assert [e["id"] for e in ents] == ["new", "old", "legacy"]
    assert ents[0]["companions"] == ["clip"] and ents[0]["file"] == "new.heic"
    assert ents[2]["file"] is None
    assert "clip" in h and len(h) == 3


def test_annotate_and_remove_with_companions(tmp_path: Path):
    path = tmp_path / "hidden.json"
    h = HiddenList(path)
    h.add("p", "c", details={"file": "x.heic"})
    h.annotate("p", archived=True)
    assert h.details("p")["archived"] is True
    d = h.remove("p")
    assert d["archived"] is True and d["file"] == "x.heic"
    assert "p" not in h and "c" not in h and len(h) == 0
    assert h.remove("p") is None
    reloaded = HiddenList(path)
    assert len(reloaded) == 0

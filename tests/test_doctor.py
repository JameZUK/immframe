from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock

from immframe.config import Config, ControlConfig, ImmichConfig, SelectionConfig, VideoConfig, ViewerConfig
from immframe.doctor import Report, check_immich, check_local, render, _check_system_config, _probe_write
from immframe.immich.client import ImmichError


def _config(**viewer) -> Config:
    return Config(
        immich=ImmichConfig(url="http://example", api_key="k"),
        selection=SelectionConfig(), video=VideoConfig(),
        viewer=ViewerConfig(raw=viewer), control=ControlConfig(),
    )


def _by_title(rep: Report, needle: str):
    return next(f for f in rep.findings if needle in f.title)


def test_unreachable_immich_is_fatal_and_short_circuits():
    client = MagicMock(); client.ping.return_value = False
    rep = Report()
    check_immich(_config(), client, rep)
    assert rep.failed
    client.random_assets.assert_not_called()


def test_permission_probes_classify_403_as_missing_permission():
    client = MagicMock()
    client.ping.return_value = True
    client.server_version.return_value = (3, 2, 0)
    client.list_people.side_effect = ImmichError("GET /people: 403 Missing required permission: person.read")
    client.list_memories.return_value = []
    client._get.return_value = {"smartSearch": True, "facialRecognition": True, "reverseGeocoding": True}
    client.search_statistics.return_value = 5
    rep = Report()
    check_immich(_config(), client, rep)
    assert _by_title(rep, "person.read").status == "warn"
    assert _by_title(rep, "asset.read").status == "ok"
    assert "3.2.0" in _by_title(rep, "Immich version").detail


def test_write_probe_distinguishes_permission_from_not_found():
    client = MagicMock()
    client.update_asset.side_effect = ImmichError("PUT /assets/x: 403 Missing required permission: asset.update")
    rep = Report(); _probe_write(client, rep)
    assert _by_title(rep, "asset.update").status == "warn"
    client.update_asset.side_effect = ImmichError("PUT /assets/x: 400 Not found or no asset.update access")
    rep = Report(); _probe_write(client, rep)
    assert _by_title(rep, "asset.update").status == "ok"


def test_system_config_recommendations():
    client = MagicMock()
    client._get.return_value = {
        "image": {"fullsize": {"enabled": False}, "preview": {"size": 1440}},
        "ffmpeg": {"transcode": "required", "targetResolution": "720"},
        "machineLearning": {"clip": {"modelName": "ViT-B-32__openai"}},
        "nightlyTasks": {"generateMemories": True},
    }
    rep = Report(); _check_system_config(client, rep)
    assert _by_title(rep, "full-size previews").status == "warn"
    assert _by_title(rep, "video transcoding").status == "warn"
    assert _by_title(rep, "CLIP model").status == "warn"
    assert _by_title(rep, "memories generated").status == "ok"

    client._get.return_value = {
        "image": {"fullsize": {"enabled": True}, "preview": {"size": 1440}},
        "ffmpeg": {"transcode": "optimal", "targetResolution": "1080"},
        "machineLearning": {"clip": {"modelName": "ViT-B-16-SigLIP2__webli"}},
    }
    rep = Report(); _check_system_config(client, rep)
    assert all(f.status == "ok" for f in rep.findings)


def test_system_config_unreadable_is_informational():
    client = MagicMock()
    client._get.side_effect = ImmichError("403 Missing required permission: systemConfig.read")
    rep = Report(); _check_system_config(client, rep)
    assert rep.findings[0].status == "info" and not rep.failed


def test_local_checks_config_perms_and_display_power(tmp_path: Path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"; cfg_file.write_text("x"); cfg_file.chmod(0o644)
    monkeypatch.setattr("immframe.doctor.shutil.which", lambda name: None)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    rep = Report()
    check_local(_config(display_power=2), rep, config_path=cfg_file)
    assert _by_title(rep, "config file permissions").status == "warn"
    assert _by_title(rep, "display_power").status == "fail"           # wlr-randr missing

    cfg_file.chmod(0o600)
    rep = Report()
    check_local(_config(display_power=0), rep, config_path=cfg_file)
    assert _by_title(rep, "config file permissions").status == "ok"
    assert _by_title(rep, "display_power").status == "warn"           # vcgencmd no-op


def test_render_marks_and_summary():
    rep = Report(); rep.ok("a"); rep.warn("b", "why", "do this"); rep.fail("c", fix="fix c"); rep.info("d")
    out = io.StringIO(); render(rep, out)
    text = out.getvalue()
    assert "✓ a" in text and "! b — why" in text and "→ do this" in text and "✗ c" in text and "· d" in text
    assert "1 problem(s), 1 recommendation(s)" in text

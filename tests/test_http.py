from __future__ import annotations

import socket
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import MagicMock

import pytest
import requests

from immframe.config import HttpConfig
from immframe.immich.models import Asset, AssetKind, GeoInfo
from immframe.interfaces.http import HttpInterface


# ── Test scaffolding ───────────────────────────────────────────────────────


def _free_port() -> int:
    """Get a free local TCP port."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StubController:
    def __init__(self) -> None:
        self.paused = False
        self.selection_mode = "random"
        self.album_ids: list[str] = ["00000000-0000-0000-0000-000000000001"]
        self.smart_query = ""
        self.current_asset: Asset | None = None
        self.next_calls = 0

    def next(self) -> None:
        self.next_calls += 1


def _asset() -> Asset:
    return Asset(
        id="abc12345-aaaa-bbbb-cccc-1234567890ab",
        kind=AssetKind.IMAGE,
        original_file_name="IMG_0001.jpg",
        mime_type="image/jpeg",
        width=4000,
        height=3000,
        taken_at=None,
        geo=GeoInfo(None, None, "Reykjavík", None, "Iceland"),
        camera_make="Canon",
        camera_model="EOS R6",
        title=None,
        caption=None,
        tag_names=(),
        favorite=False,
    )


@contextmanager
def _server(auth: bool = True, username: str = "admin", password: str = "hunter2"):
    port = _free_port()
    cfg = HttpConfig(
        enabled=True, bind="127.0.0.1", port=port,
        auth=auth, username=username, password=password,
    )
    ctrl = _StubController()
    client = MagicMock()
    iface = HttpInterface(cfg, ctrl, client)
    iface.start()
    base = f"http://127.0.0.1:{port}"
    try:
        # Wait for server to bind — first request will fail otherwise sometimes
        for _ in range(20):
            try:
                requests.get(f"{base}/healthz", timeout=0.5)
                break
            except requests.RequestException:
                continue
        yield base, ctrl, client
    finally:
        iface.stop()


def _auth() -> tuple[str, str]:
    return ("admin", "hunter2")


# ── Public endpoints ────────────────────────────────────────────────────────


def test_healthz_no_auth_required():
    with _server() as (base, _, _):
        r = requests.get(f"{base}/healthz", timeout=2.0)
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_version_requires_auth():
    with _server() as (base, _, _):
        r = requests.get(f"{base}/api/version", timeout=2.0)
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"].startswith("Basic")


def test_version_with_auth():
    with _server() as (base, _, _):
        r = requests.get(f"{base}/api/version", timeout=2.0, auth=_auth())
        assert r.status_code == 200
        assert "version" in r.json()


def test_auth_disabled_grants_all():
    with _server(auth=False) as (base, _, _):
        r = requests.get(f"{base}/api/state", timeout=2.0)
        assert r.status_code == 200


# ── State endpoint ─────────────────────────────────────────────────────────


def test_state_snapshot_shape():
    with _server() as (base, ctrl, _):
        ctrl.paused = True
        ctrl.current_asset = _asset()
        r = requests.get(f"{base}/api/state", timeout=2.0, auth=_auth())
        body = r.json()
        assert body["paused"] is True
        assert body["selection_mode"] == "random"
        assert body["current_asset"]["id"] == "abc12345-aaaa-bbbb-cccc-1234567890ab"
        assert body["current_asset"]["city"] == "Reykjavík"
        assert body["current_asset"]["camera"] == "Canon EOS R6"
        assert body["current_asset"]["kind"] == "IMAGE"


def test_state_returns_null_current_asset_initially():
    with _server() as (base, _, _):
        r = requests.get(f"{base}/api/state", timeout=2.0, auth=_auth())
        assert r.json()["current_asset"] is None


# ── Setters ────────────────────────────────────────────────────────────────


def test_post_paused_updates_controller():
    with _server() as (base, ctrl, _):
        r = requests.post(f"{base}/api/paused", json={"value": True}, timeout=2.0, auth=_auth())
        assert r.status_code == 200
        assert ctrl.paused is True
        # Response is the new state snapshot
        assert r.json()["paused"] is True


def test_post_paused_rejects_non_bool():
    with _server() as (base, ctrl, _):
        r = requests.post(f"{base}/api/paused", json={"value": "yes"}, timeout=2.0, auth=_auth())
        assert r.status_code == 400
        assert ctrl.paused is False


def test_post_paused_rejects_missing_value():
    with _server() as (base, _, _):
        r = requests.post(f"{base}/api/paused", json={}, timeout=2.0, auth=_auth())
        assert r.status_code == 400


def test_post_selection_mode():
    with _server() as (base, ctrl, _):
        r = requests.post(
            f"{base}/api/selection_mode", json={"value": "album"}, timeout=2.0, auth=_auth(),
        )
        assert r.status_code == 200
        assert ctrl.selection_mode == "album"


def test_post_selection_mode_validates_enum():
    with _server() as (base, ctrl, _):
        r = requests.post(
            f"{base}/api/selection_mode", json={"value": "everything"}, timeout=2.0, auth=_auth(),
        )
        assert r.status_code == 400
        assert ctrl.selection_mode == "random"


def test_post_album_ids():
    with _server() as (base, ctrl, _):
        uuid = "11111111-2222-3333-4444-555555555555"
        r = requests.post(
            f"{base}/api/album_ids", json={"value": [uuid]}, timeout=2.0, auth=_auth(),
        )
        assert r.status_code == 200
        assert ctrl.album_ids == [uuid]


def test_post_album_ids_rejects_path_traversal():
    with _server() as (base, ctrl, _):
        # No slashes or dots allowed by the asset-ID regex
        for bad in ["../etc/passwd", "abc/def", "a.b.c", "<script>"]:
            r = requests.post(
                f"{base}/api/album_ids", json={"value": [bad]}, timeout=2.0, auth=_auth(),
            )
            assert r.status_code == 400, f"should reject: {bad!r}"
        assert ctrl.album_ids != ["../etc/passwd"]


def test_post_album_ids_rejects_non_list():
    with _server() as (base, _, _):
        r = requests.post(
            f"{base}/api/album_ids", json={"value": "abc"}, timeout=2.0, auth=_auth(),
        )
        assert r.status_code == 400


def test_post_smart_query():
    with _server() as (base, ctrl, _):
        r = requests.post(
            f"{base}/api/smart_query", json={"value": "sunsets at the beach"}, timeout=2.0, auth=_auth(),
        )
        assert r.status_code == 200
        assert ctrl.smart_query == "sunsets at the beach"


def test_post_next_advances():
    with _server() as (base, ctrl, _):
        r = requests.post(f"{base}/api/next", timeout=2.0, auth=_auth())
        assert r.status_code == 202
        assert ctrl.next_calls == 1


# ── Allow-listing / RCE prevention ─────────────────────────────────────────


def test_unknown_endpoint_404():
    with _server() as (base, _, _):
        r = requests.get(f"{base}/api/something_else", timeout=2.0, auth=_auth())
        assert r.status_code == 404


def test_no_arbitrary_method_invocation():
    """The picframe HTTP interface allowed `getattr(controller, key)(**kwargs)`
    via query params for ANY method in `dir(controller)` — i.e. attackers
    with the auth cookie could call `stop()`, `purge_files()`, etc. The
    explicit allow-list above prevents this by construction; this test
    captures the contract.
    """
    with _server() as (base, ctrl, _):
        for path in (
            "/api/stop",
            "/api/_publish_state",
            "/api/__init__",
            "/api/_build_selector",
        ):
            r = requests.get(f"{base}{path}", timeout=2.0, auth=_auth())
            assert r.status_code == 404
            r = requests.post(f"{base}{path}", json={}, timeout=2.0, auth=_auth())
            assert r.status_code == 404


def test_method_mismatch_returns_405():
    with _server() as (base, _, _):
        r = requests.post(f"{base}/api/state", json={}, timeout=2.0, auth=_auth())
        assert r.status_code == 405
        r = requests.get(f"{base}/api/paused", timeout=2.0, auth=_auth())
        assert r.status_code == 405


# ── Image proxy ────────────────────────────────────────────────────────────


def _setup_stream_mock(client_mock, body: bytes, status: int = 200, content_type: str = "image/jpeg"):
    """Make client.stream_preview yield a fake streaming Response."""
    @contextmanager
    def fake_stream(asset_id: str) -> Iterator[MagicMock]:
        if status >= 400:
            from immframe.immich.client import ImmichError
            raise ImmichError(f"thumbnail {asset_id}: {status}")
        resp = MagicMock()
        resp.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}

        def iter_content(chunk_size: int):
            return iter([body])

        resp.iter_content.side_effect = iter_content
        yield resp

    client_mock.stream_preview.side_effect = fake_stream


def test_image_proxies_immich_bytes():
    with _server() as (base, _, client):
        _setup_stream_mock(client, b"\xff\xd8\xff\xd9JPEGBYTES")
        r = requests.get(
            f"{base}/api/image/abc12345-aaaa-bbbb-cccc-1234567890ab",
            timeout=2.0, auth=_auth(),
        )
        assert r.status_code == 200
        assert r.headers["Content-Type"] == "image/jpeg"
        assert r.content == b"\xff\xd8\xff\xd9JPEGBYTES"


def test_image_requires_auth():
    with _server() as (base, _, _):
        r = requests.get(
            f"{base}/api/image/abc12345-aaaa-bbbb-cccc-1234567890ab",
            timeout=2.0,
        )
        assert r.status_code == 401


def test_image_rejects_path_traversal_at_url_level():
    """The path regex rejects slashes — no way to compose a path-traversal URL."""
    with _server() as (base, _, _):
        # These all hit /api/image/ but with bad asset IDs
        for bad in [
            "..",
            "../../etc/passwd",
            "../../../../../../etc/passwd",
            "abc/def",
        ]:
            r = requests.get(f"{base}/api/image/{bad}", timeout=2.0, auth=_auth())
            assert r.status_code == 404, f"should not match route: {bad!r}"


def test_image_rejects_bad_asset_id_charset():
    with _server() as (base, _, _):
        for bad in ["short", "spaces here ok", "<script>"]:
            r = requests.get(f"{base}/api/image/{bad}", timeout=2.0, auth=_auth())
            assert r.status_code in (400, 404)


def test_image_upstream_404_becomes_502():
    with _server() as (base, _, client):
        _setup_stream_mock(client, b"", status=404)
        r = requests.get(
            f"{base}/api/image/abc12345-aaaa-bbbb-cccc-1234567890ab",
            timeout=2.0, auth=_auth(),
        )
        assert r.status_code == 502


# ── Body validation ────────────────────────────────────────────────────────


def test_post_rejects_non_json():
    with _server() as (base, _, _):
        r = requests.post(
            f"{base}/api/paused",
            data="not json",
            headers={"Content-Type": "text/plain", "Content-Length": "8"},
            timeout=2.0, auth=_auth(),
        )
        assert r.status_code == 400


def test_post_rejects_oversized_body():
    with _server() as (base, _, _):
        big = "x" * (128 * 1024)  # 128 KB, exceeds 64 KB cap
        r = requests.post(
            f"{base}/api/smart_query",
            data=big,
            headers={"Content-Type": "application/json"},
            timeout=2.0, auth=_auth(),
        )
        assert r.status_code == 400


# ── Constant-time auth ─────────────────────────────────────────────────────


def test_wrong_password_unauthorized():
    with _server() as (base, _, _):
        r = requests.get(f"{base}/api/state", timeout=2.0, auth=("admin", "wrong"))
        assert r.status_code == 401


def test_wrong_username_unauthorized():
    with _server() as (base, _, _):
        r = requests.get(f"{base}/api/state", timeout=2.0, auth=("nobody", "hunter2"))
        assert r.status_code == 401

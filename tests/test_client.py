from __future__ import annotations

from pathlib import Path

import pytest

responses = pytest.importorskip("responses")

from immframe.immich.client import ImmichClient, ImmichError
from immframe.immich.models import AssetKind


BASE = "https://immich.test"


def _asset_json(asset_id: str = "abc", kind: str = "IMAGE") -> dict:
    return {
        "id": asset_id,
        "type": kind,
        "originalFileName": f"{asset_id}.jpg",
        "originalMimeType": "image/jpeg",
        "width": 4000,
        "height": 3000,
        "localDateTime": "2024-06-15T12:00:00.000",
        "fileCreatedAt": "2024-06-15T12:00:00.000Z",
        "isFavorite": False,
        "exifInfo": {
            "city": "Reykjavík",
            "state": None,
            "country": "Iceland",
            "latitude": 64.13,
            "longitude": -21.94,
            "make": "Canon",
            "model": "EOS R6",
            "description": "Sunset",
        },
        "tags": [{"value": "travel"}, {"value": "europe"}],
        "checksum": "x",
        "createdAt": "2024-06-15T12:00:00Z",
        "duration": "0:00:00.000",
        "fileModifiedAt": "2024-06-15T12:00:00Z",
        "hasMetadata": True,
        "isArchived": False,
        "isEdited": False,
        "isOffline": False,
        "isTrashed": False,
        "originalPath": "/orig",
        "ownerId": "owner",
        "thumbhash": "h",
        "updatedAt": "2024-06-15T12:00:00Z",
        "visibility": "timeline",
    }


@responses.activate
def test_ping_pong():
    responses.add(responses.GET, f"{BASE}/api/server/ping", json={"res": "pong"})
    c = ImmichClient(BASE, "k")
    assert c.ping() is True


@responses.activate
def test_ping_wrong_payload_returns_false():
    responses.add(responses.GET, f"{BASE}/api/server/ping", json={"res": "nope"})
    c = ImmichClient(BASE, "k")
    assert c.ping() is False


@responses.activate
def test_ping_http_error_returns_false():
    responses.add(responses.GET, f"{BASE}/api/server/ping", status=500)
    c = ImmichClient(BASE, "k")
    assert c.ping() is False


@responses.activate
def test_random_assets_normalises():
    responses.add(responses.POST, f"{BASE}/api/search/random", json=[_asset_json("a"), _asset_json("b", "VIDEO")])
    c = ImmichClient(BASE, "k")
    assets = c.random_assets(5)
    assert len(assets) == 2
    assert assets[0].id == "a"
    assert assets[0].kind == AssetKind.IMAGE
    assert assets[0].geo.city == "Reykjavík"
    assert assets[0].geo.country == "Iceland"
    assert assets[0].camera_make == "Canon"
    assert assets[0].caption == "Sunset"
    assert assets[0].tag_names == ("travel", "europe")
    assert assets[0].taken_at is not None
    assert assets[1].kind == AssetKind.VIDEO


@responses.activate
def test_random_assets_omits_videos_when_asked():
    captured = {}

    def cb(request):
        import json
        captured["body"] = json.loads(request.body)
        return (200, {}, "[]")

    responses.add_callback(responses.POST, f"{BASE}/api/search/random", callback=cb, content_type="application/json")
    c = ImmichClient(BASE, "k")
    c.random_assets(3, with_video=False)
    assert captured["body"] == {"size": 3, "type": "IMAGE"}


@responses.activate
def test_search_smart_unwraps_items():
    responses.add(
        responses.POST,
        f"{BASE}/api/search/smart",
        json={"assets": {"items": [_asset_json("s")], "count": 1, "facets": [], "nextPage": None, "total": 1}, "albums": {"items": [], "count": 0, "facets": [], "total": 0}},
    )
    c = ImmichClient(BASE, "k")
    out = c.search_smart("beach")
    assert len(out) == 1 and out[0].id == "s"


@responses.activate
def test_album_assets_extracts_array():
    responses.add(
        responses.GET,
        f"{BASE}/api/albums/album-1",
        json={"id": "album-1", "albumName": "Trip", "assetCount": 1, "assets": [_asset_json("a")], "albumUsers": [], "createdAt": "x", "description": "", "hasSharedLink": False, "isActivityEnabled": True, "shared": False, "updatedAt": "x", "albumThumbnailAssetId": None},
    )
    c = ImmichClient(BASE, "k")
    out = c.album_assets("album-1")
    assert [a.id for a in out] == ["a"]


@responses.activate
def test_auth_header_sent():
    responses.add(responses.GET, f"{BASE}/api/server/ping", json={"res": "pong"})
    c = ImmichClient(BASE, "secret")
    c.ping()
    assert responses.calls[0].request.headers["x-api-key"] == "secret"


@responses.activate
def test_download_preview_atomic(tmp_path: Path):
    responses.add(
        responses.GET,
        f"{BASE}/api/assets/aid/thumbnail",
        body=b"\xff\xd8\xff\xd9JPEGBYTES",
        content_type="image/jpeg",
    )
    dest = tmp_path / "out.jpg"
    c = ImmichClient(BASE, "k")
    c.download_preview("aid", dest)
    assert dest.read_bytes() == b"\xff\xd8\xff\xd9JPEGBYTES"
    # No leftover .part file
    assert not (tmp_path / "out.jpg.part").exists()


@responses.activate
def test_download_preview_http_error_raises(tmp_path: Path):
    responses.add(responses.GET, f"{BASE}/api/assets/aid/thumbnail", status=404)
    c = ImmichClient(BASE, "k")
    with pytest.raises(ImmichError):
        c.download_preview("aid", tmp_path / "out.jpg")


def test_video_play_args_returns_url_and_header():
    c = ImmichClient(BASE, "k")
    url, hdrs = c.video_play_args("vid")
    assert url == f"{BASE}/api/assets/vid/video/playback"
    assert hdrs == {"x-api-key": "k"}


@responses.activate
def test_random_assets_non_list_raises():
    responses.add(responses.POST, f"{BASE}/api/search/random", json={"oops": "wrong shape"})
    c = ImmichClient(BASE, "k")
    with pytest.raises(ImmichError, match="expected list"):
        c.random_assets(3)


@responses.activate
def test_explore_returns_facet_values():
    responses.add(
        responses.GET,
        f"{BASE}/api/search/explore",
        json=[
            {"fieldName": "things", "items": [
                {"value": "beach", "data": _asset_json("a")},
                {"value": "mountain", "data": _asset_json("b")},
            ]},
            {"fieldName": "people", "items": [
                {"value": "Alice", "data": _asset_json("c")},
            ]},
        ],
    )
    c = ImmichClient(BASE, "k")
    out = c.explore()
    assert out["things"] == ["beach", "mountain"]
    assert out["people"] == ["Alice"]


@responses.activate
def test_explore_ignores_malformed_items():
    responses.add(
        responses.GET,
        f"{BASE}/api/search/explore",
        json=[
            {"fieldName": "things", "items": [
                {"value": "beach"},
                {"value": ""},
                {"no_value": "x"},
                "not a dict",
            ]},
            "garbage",
            {"items": []},  # missing fieldName
        ],
    )
    c = ImmichClient(BASE, "k")
    out = c.explore()
    assert out == {"things": ["beach"]}


@responses.activate
def test_explore_empty_when_immich_has_no_classification():
    responses.add(responses.GET, f"{BASE}/api/search/explore", json=[])
    c = ImmichClient(BASE, "k")
    assert c.explore() == {}


@responses.activate
def test_explore_non_list_raises():
    responses.add(responses.GET, f"{BASE}/api/search/explore", json={"oops": True})
    c = ImmichClient(BASE, "k")
    with pytest.raises(ImmichError):
        c.explore()


@responses.activate
def test_search_metadata_passes_filters():
    captured = {}

    def cb(request):
        import json
        captured["body"] = json.loads(request.body)
        return (200, {}, '{"assets":{"items":[],"count":0,"facets":[],"nextPage":null,"total":0},"albums":{"items":[],"count":0,"facets":[],"total":0}}')

    responses.add_callback(responses.POST, f"{BASE}/api/search/metadata", callback=cb, content_type="application/json")
    c = ImmichClient(BASE, "k")
    from datetime import datetime, timezone
    c.search_metadata(
        taken_after=datetime(2024, 1, 1, tzinfo=timezone.utc),
        country="Iceland",
        tag_ids=["t1", "t2"],
        count=10,
    )
    body = captured["body"]
    assert body["country"] == "Iceland"
    assert body["tagIds"] == ["t1", "t2"]
    assert body["size"] == 10
    assert "takenAfter" in body

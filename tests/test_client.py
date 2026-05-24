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
        "people": [
            {"id": "p1", "name": "Alice", "isHidden": False, "thumbnailPath": "", "birthDate": ""},
            {"id": "p2", "name": "Bob", "isHidden": False, "thumbnailPath": "", "birthDate": ""},
            {"id": "p3", "name": "", "isHidden": False, "thumbnailPath": "", "birthDate": ""},
            {"id": "p4", "name": "Hidden", "isHidden": True, "thumbnailPath": "", "birthDate": ""},
        ],
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
def test_live_photo_video_id_extracted():
    j = _asset_json("img-with-motion")
    j["livePhotoVideoId"] = "vid-uuid-1234"
    responses.add(responses.POST, f"{BASE}/api/search/random", json=[j])
    c = ImmichClient(BASE, "k")
    [a] = c.random_assets(1)
    assert a.live_photo_video_id == "vid-uuid-1234"


@responses.activate
def test_live_photo_video_id_none_when_absent():
    # _asset_json doesn't set livePhotoVideoId
    responses.add(responses.POST, f"{BASE}/api/search/random", json=[_asset_json("plain")])
    c = ImmichClient(BASE, "k")
    [a] = c.random_assets(1)
    assert a.live_photo_video_id is None


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
    # Only named, non-hidden people surface
    assert assets[0].people == ("Alice", "Bob")
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
    body = captured["body"]
    assert body["size"] == 3
    assert body["type"] == "IMAGE"
    # withExif and withPeople MUST be set or Immich strips that data from the
    # response — this regresses to null camera/city/country/people across
    # the whole app.
    assert body["withExif"] is True
    assert body["withPeople"] is True


@responses.activate
def test_search_smart_sets_with_flags():
    captured = {}
    def cb(request):
        import json
        captured["body"] = json.loads(request.body)
        return (200, {"Content-Type": "application/json"},
                '{"assets":{"items":[],"count":0,"facets":[],"nextPage":null,"total":0},"albums":{"items":[],"count":0,"facets":[],"total":0}}')
    responses.add_callback(responses.POST, f"{BASE}/api/search/smart", callback=cb)
    c = ImmichClient(BASE, "k")
    c.search_smart("beach", count=5)
    assert captured["body"]["withExif"] is True
    assert captured["body"]["withPeople"] is True


@responses.activate
def test_search_metadata_sets_with_flags():
    captured = {}
    def cb(request):
        import json
        captured["body"] = json.loads(request.body)
        return (200, {"Content-Type": "application/json"},
                '{"assets":{"items":[],"count":0,"facets":[],"nextPage":null,"total":0},"albums":{"items":[],"count":0,"facets":[],"total":0}}')
    responses.add_callback(responses.POST, f"{BASE}/api/search/metadata", callback=cb)
    c = ImmichClient(BASE, "k")
    c.search_metadata(city="Alrewas", count=5)
    assert captured["body"]["withExif"] is True
    assert captured["body"]["withPeople"] is True
    assert captured["body"]["city"] == "Alrewas"


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
def test_download_preview_passes_image_size(tmp_path: Path):
    captured = {}

    def cb(request):
        captured["url"] = request.url
        return (200, {"Content-Type": "image/jpeg"}, b"\xff\xd8\xff\xd9")

    responses.add_callback(responses.GET, f"{BASE}/api/assets/aid/thumbnail", callback=cb)
    c = ImmichClient(BASE, "k", image_size="fullsize")
    c.download_preview("aid", tmp_path / "out.jpg")
    assert "size=fullsize" in captured["url"]


@responses.activate
def test_download_preview_falls_back_to_preview_on_403(tmp_path: Path):
    """Common case: API key has 'view' permission but not 'download', so
    fullsize (which redirects to /original) returns 403. We should auto-
    fall-back to preview and succeed."""
    sizes_seen = []

    def cb(request):
        sizes_seen.append(request.url)
        if "size=fullsize" in request.url:
            return (403, {}, b"")
        return (200, {"Content-Type": "image/jpeg"}, b"\xff\xd8\xff\xd9JPEG")

    responses.add_callback(responses.GET, f"{BASE}/api/assets/aid/thumbnail", callback=cb)
    c = ImmichClient(BASE, "k", image_size="fullsize")
    c.download_preview("aid", tmp_path / "out.jpg")

    assert (tmp_path / "out.jpg").read_bytes() == b"\xff\xd8\xff\xd9JPEG"
    # First attempt was fullsize (403), second was preview (200)
    assert any("size=fullsize" in u for u in sizes_seen)
    assert any("size=preview" in u for u in sizes_seen)
    # Permanent switch — internal state now preview
    assert c._image_size == "preview"


@responses.activate
def test_download_preview_preview_403_still_raises(tmp_path: Path):
    """A 403 on preview is not auto-fallback territory — propagate."""
    responses.add(
        responses.GET, f"{BASE}/api/assets/aid/thumbnail", status=403,
    )
    c = ImmichClient(BASE, "k", image_size="preview")
    with pytest.raises(ImmichError, match="403"):
        c.download_preview("aid", tmp_path / "out.jpg")


def test_invalid_image_size_rejected():
    with pytest.raises(ValueError, match="image_size"):
        ImmichClient(BASE, "k", image_size="huge")


@responses.activate
def test_list_memories_returns_list_only():
    responses.add(
        responses.GET,
        f"{BASE}/api/memories",
        json=[
            {"id": "m1", "type": "on_this_day", "memoryAt": "2025-05-26T00:00:00Z",
             "data": {"year": 2020}, "assets": [_asset_json("a")]},
            "garbage",
            {"id": "m2", "type": "on_this_day", "assets": []},
        ],
    )
    c = ImmichClient(BASE, "k")
    out = c.list_memories()
    assert [m["id"] for m in out] == ["m1", "m2"]


@responses.activate
def test_get_ocr_filters_invisible_and_empty():
    responses.add(
        responses.GET,
        f"{BASE}/api/assets/aid/ocr",
        json=[
            {"text": "Hello", "isVisible": True},
            {"text": "World ", "isVisible": True},
            {"text": "  ", "isVisible": True},               # blank, skipped
            {"text": "ignored", "isVisible": False},          # not visible, skipped
            {"text": None, "isVisible": True},                # not str, skipped
        ],
    )
    c = ImmichClient(BASE, "k")
    assert c.get_ocr("aid") == ["Hello", "World"]


@responses.activate
def test_get_ocr_empty_when_endpoint_returns_unexpected_shape():
    responses.add(responses.GET, f"{BASE}/api/assets/aid/ocr", json={"oops": True})
    c = ImmichClient(BASE, "k")
    assert c.get_ocr("aid") == []


@responses.activate
def test_search_metadata_created_after():
    captured = {}
    def cb(request):
        import json
        captured["body"] = json.loads(request.body)
        return (200, {"Content-Type": "application/json"},
                '{"assets":{"items":[],"count":0,"facets":[],"nextPage":null,"total":0},"albums":{"items":[],"count":0,"facets":[],"total":0}}')
    responses.add_callback(responses.POST, f"{BASE}/api/search/metadata", callback=cb)
    c = ImmichClient(BASE, "k")
    from datetime import datetime, timezone
    c.search_metadata(
        created_after=datetime(2024, 6, 1, tzinfo=timezone.utc),
        count=10,
    )
    assert "createdAfter" in captured["body"]
    assert captured["body"]["size"] == 10


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
def test_list_people_filters_non_dict_entries():
    responses.add(
        responses.GET,
        f"{BASE}/api/people",
        json={
            "people": [
                {"id": "p1", "name": "Alice", "isHidden": False},
                {"id": "p2", "name": "", "isHidden": False},
                "garbage",
                {"id": "p3", "name": "Bob", "isHidden": True},
            ],
            "total": 4,
            "hidden": 1,
        },
    )
    c = ImmichClient(BASE, "k")
    people = c.list_people()
    # All dict entries returned; the selector decides which to use
    ids = [p["id"] for p in people]
    assert ids == ["p1", "p2", "p3"]


@responses.activate
def test_list_people_passes_with_hidden_param():
    captured = {}

    def cb(request):
        captured["query"] = request.url
        return (200, {"Content-Type": "application/json"},
                '{"people": [], "total": 0, "hidden": 0}')

    responses.add_callback(responses.GET, f"{BASE}/api/people", callback=cb)
    c = ImmichClient(BASE, "k")
    c.list_people(include_hidden=True)
    assert "withHidden=true" in captured["query"]


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
        person_ids=["pa", "pb"],
        count=10,
    )
    body = captured["body"]
    assert body["country"] == "Iceland"
    assert body["tagIds"] == ["t1", "t2"]
    assert body["personIds"] == ["pa", "pb"]
    assert body["size"] == 10
    assert "takenAfter" in body

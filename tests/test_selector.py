from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from immframe.immich.client import ImmichError
from immframe.immich.models import Asset, AssetKind, GeoInfo
from immframe.immich.selector import (
    AlbumSelector,
    CURATED_SCENE_QUERIES,
    MemorySelector,
    PeopleSelector,
    PlaylistSelector,
    RandomSelector,
    RecentSelector,
    SceneSelector,
    SmartSelector,
)


def _a(aid: str) -> Asset:
    return Asset(
        id=aid,
        kind=AssetKind.IMAGE,
        original_file_name=f"{aid}.jpg",
        mime_type="image/jpeg",
        width=100,
        height=100,
        taken_at=None,
        geo=GeoInfo(None, None, None, None, None),
        camera_make=None,
        camera_model=None,
        title=None,
        caption=None,
        tag_names=(),
        people=(),
        favorite=False,
    )


def test_random_passes_count_through():
    client = MagicMock()
    client.random_assets.return_value = [_a("x"), _a("y")]
    sel = RandomSelector(client)
    out = sel.next_batch(2)
    client.random_assets.assert_called_once_with(2, with_video=True)
    assert [a.id for a in out] == ["x", "y"]


def test_random_swallows_immich_error():
    client = MagicMock()
    client.random_assets.side_effect = ImmichError("boom")
    sel = RandomSelector(client)
    assert sel.next_batch(5) == []


def test_album_fetches_and_shuffles_pool():
    client = MagicMock()
    client.album_assets.side_effect = lambda aid: [_a(f"{aid}-1"), _a(f"{aid}-2")]
    sel = AlbumSelector(client, ["A", "B"])
    out = sel.next_batch(4)
    assert sorted(a.id for a in out) == ["A-1", "A-2", "B-1", "B-2"]
    # client called once per album
    assert client.album_assets.call_count == 2


def test_album_refills_when_exhausted():
    client = MagicMock()
    client.album_assets.side_effect = lambda aid: [_a(f"{aid}-1")]
    sel = AlbumSelector(client, ["A"])
    first = sel.next_batch(1)
    second = sel.next_batch(1)
    assert first[0].id == "A-1"
    assert second[0].id == "A-1"
    assert client.album_assets.call_count == 2


def test_album_set_ids_drains_pool():
    client = MagicMock()
    client.album_assets.side_effect = lambda aid: [_a(f"{aid}-1")]
    sel = AlbumSelector(client, ["A"])
    sel.next_batch(0)        # warm pool (no take, but builds pool)
    client.album_assets.reset_mock()
    sel.set_album_ids(["B"])
    # After set, pool is empty and refetched from new album
    out = sel.next_batch(5)
    assert client.album_assets.call_args_list == [(("B",),)]
    assert out[0].id == "B-1"


def test_album_empty_list_returns_empty():
    client = MagicMock()
    sel = AlbumSelector(client, [])
    assert sel.next_batch(5) == []
    client.album_assets.assert_not_called()


def test_smart_empty_query_returns_empty():
    client = MagicMock()
    sel = SmartSelector(client, "")
    assert sel.next_batch(5) == []
    client.search_smart.assert_not_called()


def test_smart_passes_query():
    client = MagicMock()
    client.search_smart.return_value = [_a("s")]
    sel = SmartSelector(client, "beach")
    out = sel.next_batch(3)
    client.search_smart.assert_called_once_with("beach", count=3)
    assert out[0].id == "s"


def test_smart_set_query_replaces():
    client = MagicMock()
    client.search_smart.return_value = []
    sel = SmartSelector(client, "old")
    sel.set_query("new")
    sel.next_batch(5)
    client.search_smart.assert_called_once_with("new", count=5)


# ── SceneSelector ───────────────────────────────────────────────────────


def test_scene_prefers_things_facet_when_present():
    client = MagicMock()
    client.explore.return_value = {"things": ["beach", "mountain"], "exifInfo.city": ["Paris"]}
    client.search_smart.return_value = [_a("s1"), _a("s2")]

    sel = SceneSelector(client, pool_size=5)
    batch = sel.next_batch(2)

    assert sel.mode == "things"
    client.search_smart.assert_called_once()
    chosen = client.search_smart.call_args.args[0]
    assert chosen in ("beach", "mountain")
    assert sel.current_scene == chosen
    assert {a.id for a in batch} == {"s1", "s2"}


def test_scene_uses_city_when_things_missing():
    """The bug from the field: Immich only surfaces city facets. Scene mode
    should use them via search_metadata(city=...) instead of giving up."""
    client = MagicMock()
    client.explore.return_value = {"exifInfo.city": ["Amsterdam", "Aberfeldy"]}
    client.search_metadata.return_value = [_a("city-a")]

    sel = SceneSelector(client)
    batch = sel.next_batch(5)

    assert sel.mode == "city"
    # Used the city-filter endpoint, NOT smart search
    client.search_smart.assert_not_called()
    city = client.search_metadata.call_args.kwargs["city"]
    assert city in ("Amsterdam", "Aberfeldy")
    assert batch[0].id == "city-a"


def test_scene_falls_back_to_curated_when_only_people_present():
    """People are handled by PeopleSelector now — SceneSelector ignores
    the people facet and falls back to curated CLIP queries."""
    client = MagicMock()
    client.explore.return_value = {"people": ["Alice", "Bob"]}
    client.search_smart.return_value = [_a("curated-hit")]

    sel = SceneSelector(client)
    batch = sel.next_batch(5)

    assert sel.mode == "curated"
    assert batch[0].id == "curated-hit"


def test_scene_falls_back_to_curated_when_nothing_useful():
    """When Immich exposes nothing the selector can use — but smart search
    itself still works — fall back to curated CLIP queries so the slideshow
    isn't dead in the water."""
    client = MagicMock()
    client.explore.return_value = {}
    client.list_people.return_value = []
    client.search_smart.return_value = [_a("curated-hit")]

    sel = SceneSelector(client)
    batch = sel.next_batch(5)

    assert sel.mode == "curated"
    client.search_metadata.assert_not_called()
    chosen = client.search_smart.call_args.args[0]
    assert chosen in CURATED_SCENE_QUERIES
    assert batch[0].id == "curated-hit"


def test_scene_explore_error_falls_back_to_curated():
    from immframe.immich.client import ImmichError
    client = MagicMock()
    client.explore.side_effect = ImmichError("upstream down")
    client.search_smart.return_value = [_a("ok")]

    sel = SceneSelector(client)
    batch = sel.next_batch(1)
    assert sel.mode == "curated"
    assert batch[0].id == "ok"


def test_scene_exhausts_pool_then_rotates():
    client = MagicMock()
    client.explore.return_value = {"things": ["beach"]}
    client.search_smart.side_effect = [
        [_a("a1"), _a("a2")],
        [_a("b1"), _a("b2")],
    ]

    sel = SceneSelector(client, pool_size=2)
    first = sel.next_batch(2)
    second = sel.next_batch(2)

    assert {a.id for a in first} == {"a1", "a2"}
    assert {a.id for a in second} == {"b1", "b2"}
    assert client.search_smart.call_count == 2


def test_scene_query_failure_does_not_block_subsequent():
    from immframe.immich.client import ImmichError
    client = MagicMock()
    client.explore.return_value = {"things": ["beach"]}
    client.search_smart.side_effect = ImmichError("upstream down")

    sel = SceneSelector(client, pool_size=5)
    assert sel.next_batch(5) == []
    # Recover on next call
    client.search_smart.side_effect = [[_a("ok")]]
    assert sel.next_batch(5)[0].id == "ok"


# ── PeopleSelector ──────────────────────────────────────────────────────


def test_people_explicit_ids_filters_to_those():
    client = MagicMock()
    client.list_people.return_value = [
        {"id": "p1", "name": "Alice", "isHidden": False},
        {"id": "p2", "name": "Bob", "isHidden": False},
        {"id": "px", "name": "OtherPerson", "isHidden": False},
    ]
    client.search_metadata.return_value = [_a("shot")]

    sel = PeopleSelector(client, person_ids=["p1", "p2"])
    batch = sel.next_batch(2)

    pid = client.search_metadata.call_args.kwargs["person_ids"]
    assert pid in (["p1"], ["p2"])
    assert sel.current_scene in ("Alice", "Bob")
    assert batch[0].id == "shot"


def test_people_empty_ids_rotates_all_named():
    """Empty person_ids means "rotate through every named person"."""
    client = MagicMock()
    client.list_people.return_value = [
        {"id": "p1", "name": "Alice", "isHidden": False},
        {"id": "p2", "name": "Bob", "isHidden": False},
        {"id": "p3", "name": "", "isHidden": False},        # unnamed
        {"id": "p4", "name": "Charlie", "isHidden": True},  # hidden
    ]
    client.search_metadata.return_value = [_a("shot")]

    sel = PeopleSelector(client)                # empty list
    sel.next_batch(2)
    person_ids = client.search_metadata.call_args.kwargs["person_ids"]
    # Only named, non-hidden are eligible
    assert person_ids[0] in ("p1", "p2")


def test_people_set_person_ids_drains_pool():
    client = MagicMock()
    client.list_people.return_value = [
        {"id": "p1", "name": "Alice", "isHidden": False},
        {"id": "p2", "name": "Bob", "isHidden": False},
    ]
    client.search_metadata.return_value = [_a("shot")]

    sel = PeopleSelector(client, person_ids=["p1"])
    sel.next_batch(1)
    client.search_metadata.reset_mock()

    sel.set_person_ids(["p2"])
    sel.next_batch(1)
    assert client.search_metadata.call_args.kwargs["person_ids"] == ["p2"]


def test_people_empty_library_returns_empty():
    client = MagicMock()
    client.list_people.return_value = []
    sel = PeopleSelector(client)
    assert sel.next_batch(5) == []


def test_people_metadata_error_returns_empty():
    from immframe.immich.client import ImmichError
    client = MagicMock()
    client.list_people.return_value = [
        {"id": "p1", "name": "Alice", "isHidden": False},
    ]
    client.search_metadata.side_effect = ImmichError("upstream")
    sel = PeopleSelector(client)
    assert sel.next_batch(5) == []


# ── MemorySelector ──────────────────────────────────────────────────────


def _memory(year: int, asset_ids: list[str]) -> dict:
    return {
        "id": f"mem-{year}",
        "type": "on_this_day",
        "memoryAt": f"{year}-05-26T00:00:00Z",
        "data": {"year": year},
        "assets": [{
            "id": aid, "type": "IMAGE", "originalFileName": f"{aid}.jpg",
            "originalMimeType": "image/jpeg", "width": 100, "height": 100,
            "localDateTime": "2020-05-26T12:00:00Z", "fileCreatedAt": "2020-05-26T12:00:00Z",
            "isFavorite": False, "exifInfo": {}, "people": [], "tags": [],
            "checksum": "x", "createdAt": "x", "duration": "0:00:00",
            "fileModifiedAt": "x", "hasMetadata": True, "isArchived": False,
            "isEdited": False, "isOffline": False, "isTrashed": False,
            "originalPath": "/", "ownerId": "u", "thumbhash": "h",
            "updatedAt": "x", "visibility": "timeline",
        } for aid in asset_ids],
    }


def test_memory_picks_random_memory_and_shows_its_assets():
    client = MagicMock()
    client.list_memories.return_value = [
        _memory(2020, ["a1", "a2"]),
        _memory(2021, ["b1", "b2"]),
    ]
    sel = MemorySelector(client)
    batch = sel.next_batch(4)
    # All assets from one memory (whichever was picked)
    ids = {a.id for a in batch}
    assert ids in ({"a1", "a2"}, {"b1", "b2"})
    assert sel.current_scene and "year" in sel.current_scene.lower()


def test_memory_empty_list_silent():
    client = MagicMock()
    client.list_memories.return_value = []
    sel = MemorySelector(client)
    assert sel.next_batch(5) == []


def test_memory_error_returns_empty():
    from immframe.immich.client import ImmichError
    client = MagicMock()
    client.list_memories.side_effect = ImmichError("down")
    sel = MemorySelector(client)
    assert sel.next_batch(5) == []


# ── RecentSelector ──────────────────────────────────────────────────────


def test_recent_uses_created_after_by_default():
    client = MagicMock()
    client.search_metadata.return_value = [_a("r1"), _a("r2")]
    sel = RecentSelector(client, days=14)
    batch = sel.next_batch(5)
    assert {a.id for a in batch} == {"r1", "r2"}
    # Verify it asked for createdAfter, not takenAfter
    kwargs = client.search_metadata.call_args.kwargs
    assert "created_after" in kwargs
    assert "taken_after" not in kwargs


def test_recent_with_taken_field():
    client = MagicMock()
    client.search_metadata.return_value = [_a("r")]
    sel = RecentSelector(client, days=7, field="taken")
    sel.next_batch(5)
    kwargs = client.search_metadata.call_args.kwargs
    assert "taken_after" in kwargs
    assert "created_after" not in kwargs


def test_recent_rejects_bad_field():
    client = MagicMock()
    with pytest.raises(ValueError, match="field"):
        RecentSelector(client, field="yesterday")


def test_recent_exposes_friendly_label():
    sel = RecentSelector(MagicMock(), days=14)
    assert "14" in (sel.current_scene or "")


# ── PlaylistSelector ────────────────────────────────────────────────────


def test_playlist_rotates_through_entries():
    s1 = MagicMock()
    s1.next_batch.side_effect = [[_a("s1-1"), _a("s1-2")], [_a("s1-3"), _a("s1-4")]]
    s2 = MagicMock()
    s2.next_batch.return_value = [_a("s2-1"), _a("s2-2")]

    sel = PlaylistSelector([(s1, 4), (s2, 2)])

    # First two calls drain s1's 4-item quota
    out1 = sel.next_batch(2)
    out2 = sel.next_batch(2)
    assert {a.id for a in out1} == {"s1-1", "s1-2"}
    assert {a.id for a in out2} == {"s1-3", "s1-4"}

    # s1 exhausted, advance to s2
    out3 = sel.next_batch(2)
    assert {a.id for a in out3} == {"s2-1", "s2-2"}


def test_playlist_advances_when_sub_returns_empty():
    s1 = MagicMock()
    s1.next_batch.return_value = []   # always empty
    s2 = MagicMock()
    s2.next_batch.return_value = [_a("s2-1")]
    sel = PlaylistSelector([(s1, 10), (s2, 10)])
    out = sel.next_batch(5)
    assert out[0].id == "s2-1"


def test_playlist_returns_empty_when_all_subs_empty():
    s1 = MagicMock()
    s1.next_batch.return_value = []
    s2 = MagicMock()
    s2.next_batch.return_value = []
    sel = PlaylistSelector([(s1, 5), (s2, 5)])
    assert sel.next_batch(5) == []


def test_playlist_empty_entries_raises():
    with pytest.raises(ValueError):
        PlaylistSelector([])


def test_playlist_current_scene_proxies_active_sub():
    s1 = MagicMock()
    s1.current_scene = "beach"
    s1.next_batch.return_value = [_a("x")]
    sel = PlaylistSelector([(s1, 10)])
    assert sel.current_scene == "beach"


def test_scene_force_mode_skips_discovery():
    """Useful for tests and explicit user preference."""
    client = MagicMock()
    client.search_smart.return_value = [_a("forced")]

    sel = SceneSelector(client, force_mode="curated")
    batch = sel.next_batch(1)
    assert sel.mode == "curated"
    client.explore.assert_not_called()
    client.list_people.assert_not_called()
    assert batch[0].id == "forced"

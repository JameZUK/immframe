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
        live_photo_video_id=None,
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
    sel = SmartSelector(client, "beach", pages=1)
    out = sel.next_batch(3)
    client.search_smart.assert_called_once_with("beach", count=3, page=1)
    assert out[0].id == "s"


def test_smart_set_query_replaces():
    client = MagicMock()
    client.search_smart.return_value = [_a("s")]
    sel = SmartSelector(client, "old", pages=1)
    sel.set_query("new")
    sel.next_batch(5)
    client.search_smart.assert_called_once_with("new", count=5, page=1)


def test_smart_samples_pages_and_falls_back_to_page_1():
    """CLIP ranking is deterministic — sample a random page from the top
    `pages` so a query doesn't show the same N photos forever; an empty
    later page (few matches) falls back to page 1."""
    client = MagicMock()
    client.search_smart.side_effect = lambda q, count, page: [_a(f"p{page}")] if page == 1 else []
    sel = SmartSelector(client, "beach", pages=4)
    for _ in range(40):
        out = sel.next_batch(2)
        assert out and out[0].id == "p1"                # fallback delivered
    pages_seen = {c.kwargs["page"] for c in client.search_smart.call_args_list}
    assert pages_seen == {1, 2, 3, 4}


def test_random_selector_passes_filters():
    client = MagicMock()
    client.random_assets.return_value = [_a("f")]
    sel = RandomSelector(client, favorites=True, min_rating=4, album_ids=["alb"], tag_ids=["t"])
    sel.next_batch(3)
    client.random_assets.assert_called_once_with(
        3, with_video=True, favorites=True, min_rating=4, album_ids=["alb"], tag_ids=["t"],
    )
    assert sel.current_scene == "Favourites"


def test_filtered_random_signals_exhaustion_after_short_batch():
    """One starred photo + `{mode: favorites, count: 5}` must not show that
    photo five times: a short batch yields [] next so a playlist advances."""
    client = MagicMock()
    client.random_assets.return_value = [_a("only")]
    sel = RandomSelector(client, favorites=True)
    assert [a.id for a in sel.next_batch(5)] == ["only"]
    assert sel.next_batch(5) == []
    assert [a.id for a in sel.next_batch(5)] == ["only"]        # new round
    other = MagicMock(); other.next_batch.side_effect = lambda n: [_a("o")] * n
    pl = PlaylistSelector([(RandomSelector(client, favorites=True), 5), (other, 2)])
    assert [a.id for a in pl.next_batch(5)] == ["only"]
    assert [a.id for a in pl.next_batch(5)] == ["o", "o"]


def test_unfiltered_random_never_signals_exhaustion():
    client = MagicMock()
    client.random_assets.return_value = [_a("x")]              # tiny library
    sel = RandomSelector(client)
    assert sel.next_batch(5) and sel.next_batch(5) and sel.next_batch(5)


def test_random_selector_plain_has_no_label():
    assert RandomSelector(MagicMock()).current_scene is None
    assert RandomSelector(MagicMock(), min_rating=3).current_scene == "Rated 3+"


# ── SceneSelector ───────────────────────────────────────────────────────


def test_scene_prefers_things_facet_when_present():
    client = MagicMock()
    client.explore.return_value = {"things": ["beach", "mountain"], "exifInfo.city": ["Paris"]}
    client.search_smart.return_value = [_a("s1"), _a("s2"), _a("s3")]

    sel = SceneSelector(client, pool_size=5)
    batch = sel.next_batch(3)

    assert sel.mode == "things"
    client.list_cities.assert_not_called()
    client.search_smart.assert_called_once()
    chosen = client.search_smart.call_args.args[0]
    assert chosen in ("beach", "mountain")
    assert sel.current_scene == chosen
    assert {a.id for a in batch} == {"s1", "s2", "s3"}


def _pool(prefix: str, n: int = 3) -> list:
    return [_a(f"{prefix}{i}") for i in range(n)]


def test_scene_uses_full_city_list_when_things_missing():
    """The bug from the field: /search/explore's city facet is capped at 12
    alphabetically-first entries, so a big library only ever rotated through
    its "A" cities. Scene mode must use the full /search/cities list."""
    client = MagicMock()
    client.explore.return_value = {"exifInfo.city": ["Aberfeldy", "Abersoch"]}   # the capped facet
    client.list_cities.return_value = ["Aberfeldy", "Abersoch", "York", "Zaandijk"]
    client.random_assets.return_value = _pool("city-")

    sel = SceneSelector(client)
    batch = sel.next_batch(5)

    assert sel.mode == "city"
    # Used a city-filtered random sample, NOT smart search
    client.search_smart.assert_not_called()
    city = client.random_assets.call_args.kwargs["city"]
    assert city in ("Aberfeldy", "Abersoch", "York", "Zaandijk")
    assert sel.current_scene == city
    assert batch[0].id.startswith("city-")


def test_scene_city_labels_span_full_list():
    client = MagicMock()
    client.explore.return_value = {}
    client.list_cities.return_value = [f"c{i}" for i in range(50)]
    client.random_assets.return_value = _pool("x")

    sel = SceneSelector(client, pool_size=3)
    seen = set()
    for _ in range(60):
        sel.next_batch(3)                        # each call drains the pool → rotates
        seen.add(sel.current_scene)
    assert len(seen) > 12                        # well beyond explore's cap


def test_scene_falls_back_to_explore_city_facet_without_cities_endpoint():
    """Older Immich: /search/cities fails → use whatever explore gives."""
    client = MagicMock()
    client.explore.return_value = {"exifInfo.city": ["Amsterdam", "Aberfeldy"]}
    client.list_cities.side_effect = ImmichError("404")
    client.random_assets.return_value = _pool("f")

    sel = SceneSelector(client)
    sel.next_batch(5)

    assert sel.mode == "city"
    assert client.random_assets.call_args.kwargs["city"] in ("Amsterdam", "Aberfeldy")


def test_scene_explore_error_still_tries_cities():
    client = MagicMock()
    client.explore.side_effect = ImmichError("upstream down")
    client.list_cities.return_value = ["York"]
    client.random_assets.return_value = _pool("y")

    sel = SceneSelector(client)
    batch = sel.next_batch(5)
    assert sel.mode == "city"
    assert batch[0].id.startswith("y")


def test_scene_city_resamples_each_rotation():
    """/search/metadata returns the same newest-first page for a city every
    time; a random sample must be drawn per rotation instead."""
    client = MagicMock()
    client.explore.return_value = {}
    client.list_cities.return_value = ["Amsterdam"]
    client.random_assets.side_effect = [_pool("a"), _pool("b")]

    sel = SceneSelector(client, pool_size=3)
    first = sel.next_batch(3)
    second = sel.next_batch(3)

    assert {a.id for a in first} == {"a0", "a1", "a2"}
    assert {a.id for a in second} == {"b0", "b1", "b2"}
    assert client.random_assets.call_count == 2
    client.search_metadata.assert_not_called()


def test_scene_skips_thin_city_pools():
    """A one-photo city is neither a scene nor a collage: draw another label
    (bounded) before settling."""
    client = MagicMock()
    client.explore.return_value = {}
    client.list_cities.return_value = ["Tiny", "Big"]
    client.random_assets.side_effect = lambda n, **kw: (
        _pool("big", 5) if kw["city"] == "Big" else [_a("only-one")]
    )
    # Bias random.choice toward "Tiny" first, then "Big"
    import random as _random
    choices = iter(["Tiny", "Big"])
    orig = _random.choice
    _random.choice = lambda seq: next(choices)
    try:
        sel = SceneSelector(client, pool_size=5)
        batch = sel.next_batch(5)
    finally:
        _random.choice = orig

    assert sel.current_scene == "Big"
    assert len(batch) == 5


def test_scene_thin_pool_retry_is_bounded():
    client = MagicMock()
    client.explore.return_value = {}
    client.list_cities.return_value = ["A", "B", "C", "D", "E", "F"]
    client.random_assets.return_value = [_a("solo")]        # every city is thin

    sel = SceneSelector(client, pool_size=5)
    batch = sel.next_batch(5)

    assert client.random_assets.call_count == SceneSelector.MAX_LABEL_TRIES
    assert [a.id for a in batch] == ["solo"]                # still serves what it got


def test_scene_falls_back_to_curated_when_only_people_present():
    """People are handled by PeopleSelector now — SceneSelector ignores
    the people facet and falls back to curated CLIP queries."""
    client = MagicMock()
    client.explore.return_value = {"people": ["Alice", "Bob"]}
    client.list_cities.return_value = []
    client.search_smart.return_value = _pool("curated-hit")

    sel = SceneSelector(client)
    batch = sel.next_batch(5)

    assert sel.mode == "curated"
    assert batch[0].id.startswith("curated-hit")


def test_scene_falls_back_to_curated_when_nothing_useful():
    """When Immich exposes nothing the selector can use — but smart search
    itself still works — fall back to curated CLIP queries so the slideshow
    isn't dead in the water."""
    client = MagicMock()
    client.explore.return_value = {}
    client.list_cities.return_value = []
    client.search_smart.return_value = _pool("curated-hit")

    sel = SceneSelector(client)
    batch = sel.next_batch(5)

    assert sel.mode == "curated"
    client.search_metadata.assert_not_called()
    client.random_assets.assert_not_called()
    chosen = client.search_smart.call_args.args[0]
    assert chosen in CURATED_SCENE_QUERIES
    assert batch[0].id.startswith("curated-hit")


def test_scene_explore_and_cities_errors_fall_back_to_curated():
    client = MagicMock()
    client.explore.side_effect = ImmichError("upstream down")
    client.list_cities.side_effect = ImmichError("upstream down")
    client.search_smart.return_value = _pool("ok")

    sel = SceneSelector(client)
    batch = sel.next_batch(1)
    assert sel.mode == "curated"
    assert batch[0].id.startswith("ok")


def test_scene_exhausts_pool_then_rotates():
    client = MagicMock()
    client.explore.return_value = {"things": ["beach"]}
    client.search_smart.side_effect = [_pool("a"), _pool("b")]

    sel = SceneSelector(client, pool_size=3)
    first = sel.next_batch(3)
    second = sel.next_batch(3)

    assert {a.id for a in first} == {"a0", "a1", "a2"}
    assert {a.id for a in second} == {"b0", "b1", "b2"}
    assert client.search_smart.call_count == 2


def test_scene_query_failure_does_not_block_subsequent():
    from immframe.immich.client import ImmichError
    client = MagicMock()
    client.explore.return_value = {"things": ["beach"]}
    client.search_smart.side_effect = ImmichError("upstream down")

    sel = SceneSelector(client, pool_size=5)
    assert sel.next_batch(5) == []
    # Recover on next call
    client.search_smart.side_effect = None
    client.search_smart.return_value = _pool("ok")
    assert sel.next_batch(5)[0].id.startswith("ok")


# ── PeopleSelector ──────────────────────────────────────────────────────


def test_people_explicit_ids_filters_to_those():
    client = MagicMock()
    client.list_people.return_value = [
        {"id": "p1", "name": "Alice", "isHidden": False},
        {"id": "p2", "name": "Bob", "isHidden": False},
        {"id": "px", "name": "OtherPerson", "isHidden": False},
    ]
    client.random_assets.return_value = [_a("shot")]

    sel = PeopleSelector(client, person_ids=["p1", "p2"])
    batch = sel.next_batch(2)

    pid = client.random_assets.call_args.kwargs["person_ids"]
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
    client.random_assets.return_value = [_a("shot")]

    sel = PeopleSelector(client)                # empty list
    sel.next_batch(2)
    person_ids = client.random_assets.call_args.kwargs["person_ids"]
    # Only named, non-hidden are eligible
    assert person_ids[0] in ("p1", "p2")


def test_people_set_person_ids_drains_pool():
    client = MagicMock()
    client.list_people.return_value = [
        {"id": "p1", "name": "Alice", "isHidden": False},
        {"id": "p2", "name": "Bob", "isHidden": False},
    ]
    client.random_assets.return_value = [_a("shot")]

    sel = PeopleSelector(client, person_ids=["p1"])
    sel.next_batch(1)
    client.random_assets.reset_mock()

    sel.set_person_ids(["p2"])
    sel.next_batch(1)
    assert client.random_assets.call_args.kwargs["person_ids"] == ["p2"]


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
    client.random_assets.side_effect = ImmichError("upstream")
    sel = PeopleSelector(client)
    assert sel.next_batch(5) == []


def test_people_resamples_each_rotation():
    """Each rotation is a fresh random sample of the person, not the same
    newest-first page from /search/metadata."""
    client = MagicMock()
    client.list_people.return_value = [{"id": "p1", "name": "Alice", "isHidden": False}]
    client.random_assets.side_effect = [[_a("a1"), _a("a2")], [_a("b1"), _a("b2")]]

    sel = PeopleSelector(client, person_ids=["p1"], pool_size=2)
    first = sel.next_batch(2)
    second = sel.next_batch(2)

    assert {a.id for a in first} == {"a1", "a2"}
    assert {a.id for a in second} == {"b1", "b2"}
    assert client.random_assets.call_args.args[0] == 2          # pool_size
    client.search_metadata.assert_not_called()


def test_people_min_photos_skips_thin_people():
    """Auto-rotation with people_min_photos: draw until someone with enough
    photos comes up; counts via /search/statistics, cached."""
    client = MagicMock()
    client.list_people.return_value = [
        {"id": "thin", "name": "Thin", "isHidden": False},
        {"id": "big", "name": "Big", "isHidden": False},
    ]
    client.search_statistics.side_effect = lambda person_ids: {"thin": 3, "big": 900}[person_ids[0]]
    client.random_assets.return_value = [_a("shot")]

    sel = PeopleSelector(client, min_photos=20, pool_size=1)
    for _ in range(10):
        sel.next_batch(1)
        assert sel.current_scene == "Big"
    # Each person counted at most once.
    assert client.search_statistics.call_count <= 2


def test_people_min_photos_not_applied_to_explicit_ids():
    client = MagicMock()
    client.list_people.return_value = [{"id": "p1", "name": "Alice", "isHidden": False}]
    client.random_assets.return_value = [_a("shot")]
    sel = PeopleSelector(client, person_ids=["p1"], min_photos=1000)
    assert sel.next_batch(1)
    client.search_statistics.assert_not_called()


def test_people_min_photos_falls_back_to_largest_when_all_thin():
    client = MagicMock()
    client.list_people.return_value = [
        {"id": "a", "name": "A", "isHidden": False},
        {"id": "b", "name": "B", "isHidden": False},
    ]
    client.search_statistics.side_effect = lambda person_ids: {"a": 3, "b": 7}[person_ids[0]]
    client.random_assets.return_value = [_a("shot")]
    sel = PeopleSelector(client, min_photos=50, pool_size=1)
    sel.next_batch(1)
    assert sel.current_scene == "B"


def test_people_statistics_error_still_rotates():
    client = MagicMock()
    client.list_people.return_value = [{"id": "a", "name": "A", "isHidden": False}]
    client.search_statistics.side_effect = ImmichError("no permission")
    client.random_assets.return_value = [_a("shot")]
    sel = PeopleSelector(client, min_photos=50)
    assert sel.next_batch(1)[0].id == "shot"


def test_people_favorites_only_prefers_starred():
    client = MagicMock()
    client.list_people.return_value = [
        {"id": "p1", "name": "Alice", "isHidden": False, "isFavorite": False},
        {"id": "p2", "name": "Bob", "isHidden": False, "isFavorite": True},
    ]
    client.random_assets.return_value = [_a("shot")]
    sel = PeopleSelector(client, favorites_only=True, pool_size=1)
    for _ in range(5):
        sel.next_batch(1)
        assert sel.current_scene == "Bob"


def test_people_favorites_only_falls_back_when_none_starred():
    client = MagicMock()
    client.list_people.return_value = [
        {"id": "p1", "name": "Alice", "isHidden": False, "isFavorite": False},
    ]
    client.random_assets.return_value = [_a("shot")]
    sel = PeopleSelector(client, favorites_only=True)
    sel.next_batch(1)
    assert sel.current_scene == "Alice"


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
    client.random_assets.return_value = [_a("r1"), _a("r2")]
    sel = RecentSelector(client, days=14)
    batch = sel.next_batch(5)
    assert {a.id for a in batch} == {"r1", "r2"}
    # Verify it asked for createdAfter, not takenAfter — and via the random
    # endpoint, not the fixed-order metadata search.
    kwargs = client.random_assets.call_args.kwargs
    assert "created_after" in kwargs
    assert "taken_after" not in kwargs
    client.search_metadata.assert_not_called()


def test_recent_with_taken_field():
    client = MagicMock()
    client.random_assets.return_value = [_a("r")]
    sel = RecentSelector(client, days=7, field="taken")
    sel.next_batch(5)
    kwargs = client.random_assets.call_args.kwargs
    assert "taken_after" in kwargs
    assert "created_after" not in kwargs


def test_recent_serves_pool_without_replacement_then_resamples():
    """One query per rotation (pool_size), served across batches; a fresh
    random sample once the pool runs dry."""
    client = MagicMock()
    client.random_assets.side_effect = [
        [_a("a1"), _a("a2"), _a("a3"), _a("a4")],
        [_a("b1"), _a("b2"), _a("b3"), _a("b4")],
    ]
    sel = RecentSelector(client, days=7, pool_size=4)
    first = sel.next_batch(2)
    second = sel.next_batch(2)
    assert client.random_assets.call_count == 1
    assert {a.id for a in first} | {a.id for a in second} == {"a1", "a2", "a3", "a4"}
    assert not ({a.id for a in first} & {a.id for a in second})
    third = sel.next_batch(2)
    assert client.random_assets.call_count == 2
    assert client.random_assets.call_args.args[0] == 4          # pool_size
    assert {a.id for a in third} <= {"b1", "b2", "b3", "b4"}


def test_recent_small_window_signals_exhaustion_once():
    """A short sample means the window is smaller than pool_size — after
    serving all of it, return [] once (so a playlist advances) rather than
    immediately replaying the same few photos."""
    client = MagicMock()
    client.random_assets.return_value = [_a("x"), _a("y"), _a("z")]
    sel = RecentSelector(client, days=7, pool_size=25)
    assert {a.id for a in sel.next_batch(5)} == {"x", "y", "z"}
    assert sel.next_batch(5) == []
    assert client.random_assets.call_count == 1
    # ...and then a new rotation starts.
    assert {a.id for a in sel.next_batch(5)} == {"x", "y", "z"}
    assert client.random_assets.call_count == 2


def test_recent_full_window_does_not_signal_exhaustion():
    client = MagicMock()
    client.random_assets.return_value = [_a("x"), _a("y"), _a("z")]
    sel = RecentSelector(client, days=7, pool_size=3)              # sample == pool_size
    assert len(sel.next_batch(5)) == 3
    assert len(sel.next_batch(5)) == 3                             # refilled, no []


def test_recent_error_returns_empty_then_recovers():
    client = MagicMock()
    client.random_assets.side_effect = [ImmichError("upstream"), [_a("ok")]]
    sel = RecentSelector(client, days=7)
    assert sel.next_batch(5) == []
    assert sel.next_batch(5)[0].id == "ok"


def test_playlist_recent_entry_advances_when_window_is_small():
    """The field bug: `{mode: recent, count: 10}` with only 3 uploads in the
    window used to re-show those 3 photos until the count was met (and the
    collage entry after it tiled the same 3 again). Now the entry yields its
    3 and the playlist moves on."""
    recent_client = MagicMock()
    recent_client.random_assets.return_value = [_a("r1"), _a("r2"), _a("r3")]
    recent = RecentSelector(recent_client, days=7, pool_size=25)
    other = MagicMock()
    other.next_batch.side_effect = lambda n: [_a("other")] * n

    pl = PlaylistSelector([(recent, 10), (other, 2)])
    assert {a.id for a in pl.next_batch(5)} == {"r1", "r2", "r3"}
    assert [a.id for a in pl.next_batch(5)] == ["other", "other"]   # advanced, quota 2
    assert recent_client.random_assets.call_count == 1


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


def test_playlist_collage_active_defaults_false_for_legacy_entries():
    s1 = MagicMock()
    s1.next_batch.return_value = [_a("x")]
    sel = PlaylistSelector([(s1, 5)])           # 2-tuple, backward compatible
    assert sel.collage_active() is False


def test_playlist_collage_entry_counts_by_collage_not_assets():
    from immframe.config import CollageConfig
    cc = CollageConfig(enabled=True)
    s1 = MagicMock()
    s1.next_batch.return_value = [_a("a"), _a("b"), _a("c")]
    s2 = MagicMock()
    s2.next_batch.return_value = [_a("z")]
    # entry 0: 2 collages; entry 1: 1 single photo
    sel = PlaylistSelector([(s1, 2, cc), (s2, 1, None)])

    assert sel.collage_active() is True
    assert sel.current_collage() is cc
    b1 = sel.next_batch(3)                       # collage 1 of 2
    assert {a.id for a in b1} == {"a", "b", "c"}
    assert sel.collage_active() is True
    sel.next_batch(3)                            # collage 2 of 2 → quota hit, advance
    assert sel.collage_active() is False         # now on the single entry
    assert sel.current_collage() is None
    b3 = sel.next_batch(3)
    assert b3[0].id == "z"


def test_playlist_mixes_singles_and_collages():
    from immframe.config import CollageConfig
    s1 = MagicMock()
    s1.next_batch.return_value = [_a("p1"), _a("p2")]
    sel = PlaylistSelector([(s1, 2, None), (s1, 1, CollageConfig(enabled=True))])
    # First entry: counts photos (2 per call → quota 2 in one call)
    sel.next_batch(2)
    assert sel.collage_active() is True          # advanced to the collage entry


def test_scene_force_mode_skips_discovery():
    """Useful for tests and explicit user preference."""
    client = MagicMock()
    client.search_smart.return_value = _pool("forced")

    sel = SceneSelector(client, force_mode="curated")
    batch = sel.next_batch(1)
    assert sel.mode == "curated"
    client.explore.assert_not_called()
    client.list_cities.assert_not_called()
    assert batch[0].id.startswith("forced")

from __future__ import annotations

from unittest.mock import MagicMock

from immframe.immich.client import ImmichError
from immframe.immich.models import Asset, AssetKind, GeoInfo
from immframe.immich.selector import (
    AlbumSelector,
    RandomSelector,
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


def test_scene_picks_random_scene_from_explore_and_queries_it():
    client = MagicMock()
    client.explore.return_value = {"things": ["beach", "mountain"]}
    client.search_smart.return_value = [_a("s1"), _a("s2")]

    sel = SceneSelector(client, pool_size=5)
    batch = sel.next_batch(2)

    client.explore.assert_called_once()
    client.search_smart.assert_called_once()
    # The scene query goes to whichever scene was picked
    chosen = client.search_smart.call_args.args[0]
    assert chosen in ("beach", "mountain")
    assert sel.current_scene == chosen
    assert {a.id for a in batch} == {"s1", "s2"}


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
    # Two smart-search calls = two scene rotations
    assert client.search_smart.call_count == 2
    # Explore only called once — scene list is cached
    assert client.explore.call_count == 1


def test_scene_empty_explore_returns_empty():
    client = MagicMock()
    client.explore.return_value = {}
    sel = SceneSelector(client)
    assert sel.next_batch(5) == []


def test_scene_explore_error_returns_empty():
    from immframe.immich.client import ImmichError
    client = MagicMock()
    client.explore.side_effect = ImmichError("network down")
    sel = SceneSelector(client)
    assert sel.next_batch(5) == []


def test_scene_smart_search_error_returns_empty_and_keeps_running():
    from immframe.immich.client import ImmichError
    client = MagicMock()
    client.explore.return_value = {"things": ["beach"]}
    client.search_smart.side_effect = ImmichError("upstream down")
    sel = SceneSelector(client, pool_size=5)
    assert sel.next_batch(5) == []
    # Subsequent call retries
    client.search_smart.side_effect = [[_a("ok")]]
    assert sel.next_batch(5)[0].id == "ok"


def test_scene_respects_field_name():
    client = MagicMock()
    client.explore.return_value = {"people": ["Alice", "Bob"]}
    client.search_smart.return_value = [_a("p")]

    sel = SceneSelector(client, field_name="people")
    sel.next_batch(1)
    # search_smart called with one of the people values
    assert client.search_smart.call_args.args[0] in ("Alice", "Bob")

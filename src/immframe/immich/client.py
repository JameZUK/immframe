"""Thin synchronous wrapper around the Immich REST API.

Targets Immich's current API (verified against open-api/immich-openapi-specs.json
at the time of writing). Endpoint paths and JSON-shape mapping live ONLY in
this file; if Immich changes its API between versions, this is the only file
to edit.

Auth via the `x-api-key` header. All methods raise `ImmichError` on failure.
Callers in the prefetch worker should catch broadly and log so a transient
network blip doesn't crash the slideshow.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

from .models import Asset, AssetKind, GeoInfo

log = logging.getLogger(__name__)


class ImmichError(Exception):
    """Raised for any Immich client failure (network, auth, server, parse)."""


class ImmichClient:
    """Synchronous Immich API client.

    Thread-safe: uses a single `requests.Session` with the API key as a default
    header. Concurrent calls from the prefetch worker and (Phase 2) control
    plane are fine.
    """

    AUTH_HEADER = "x-api-key"
    _API_PREFIX = "/api"

    # AssetMediaSize enum. `fullsize` returns the original-resolution JPEG
    # (transcoded for HEIC/RAW), 302-redirecting to /assets/{id}/original
    # behind the scenes — requests follows redirects by default.
    SIZE_PREVIEW = "preview"              # ~1440x2560
    SIZE_FULLSIZE = "fullsize"            # original resolution, JPEG
    VALID_IMAGE_SIZES = frozenset({SIZE_PREVIEW, SIZE_FULLSIZE})

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 10.0,
        session: requests.Session | None = None,
        image_size: str = SIZE_FULLSIZE,
    ) -> None:
        self._base = base_url.rstrip("/") + self._API_PREFIX
        self._api_key = api_key
        self._timeout = timeout_s
        if image_size not in self.VALID_IMAGE_SIZES:
            raise ValueError(
                f"image_size must be one of {sorted(self.VALID_IMAGE_SIZES)}; got {image_size!r}"
            )
        self._image_size = image_size
        if session is None:
            self._session = requests.Session()
            self._owns_session = True
        else:
            self._session = session
            self._owns_session = False
        self._session.headers.setdefault(self.AUTH_HEADER, api_key)
        self._session.headers.setdefault("Accept", "application/json")
        # Server version, resolved lazily on the first search and cached.
        # Decides the search-filter dialect (see _search_body).
        self._version: tuple[int, int, int] | None = None
        self._version_lock = threading.Lock()

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    # ── HTTP helpers ────────────────────────────────────────────────────
    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self._base + path

    def _get(self, path: str, **kw: Any) -> Any:
        return self._request("GET", path, **kw)

    def _post(self, path: str, *, json: Any = None, **kw: Any) -> Any:
        return self._request("POST", path, json=json, **kw)

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        kw.setdefault("timeout", self._timeout)
        try:
            r = self._session.request(method, self._url(path), **kw)
        except requests.RequestException as e:
            raise ImmichError(f"{method} {path}: {e}") from e
        if r.status_code >= 400:
            raise ImmichError(f"{method} {path}: {r.status_code} {r.text[:200]}")
        if not r.content:
            return None
        ct = r.headers.get("Content-Type", "")
        if "application/json" in ct:
            try:
                return r.json()
            except ValueError as e:
                raise ImmichError(f"{method} {path}: non-JSON body ({e})") from e
        return r.content

    # ── Health ──────────────────────────────────────────────────────────
    def ping(self) -> bool:
        """True iff `/server/ping` returns `{"res": "pong"}`. Never raises."""
        try:
            data = self._get("/server/ping")
        except ImmichError as e:
            log.warning("ping failed: %s", e)
            return False
        return isinstance(data, dict) and data.get("res") == "pong"

    def server_version(self) -> tuple[int, int, int] | None:
        """GET /server/version as (major, minor, patch), cached for the
        life of the client. None if the endpoint is unreachable or the
        response is unparseable — callers treat that as "old server"."""
        with self._version_lock:
            if self._version is not None:
                return self._version
            try:
                data = self._get("/server/version")
            except ImmichError as e:
                log.debug("server version lookup failed: %s", e)
                return None
            if not isinstance(data, dict):
                return None
            try:
                v = (int(data["major"]), int(data["minor"]), int(data["patch"]))
            except (KeyError, TypeError, ValueError):
                return None
            self._version = v
            log.info("Immich server v%d.%d.%d (search dialect: %s)",
                     *v, "structured filter" if v >= STRUCTURED_FILTER_SINCE else "flat fields")
            return v

    @property
    def structured_filters(self) -> bool:
        """True when the server takes the v3.2+ `filter` object; the flat
        filter fields are deprecated there and slated for removal."""
        v = self.server_version()
        return v is not None and v >= STRUCTURED_FILTER_SINCE

    # ── Asset selection ─────────────────────────────────────────────────
    def random_assets(
        self,
        count: int,
        *,
        with_video: bool = True,
        taken_after: datetime | None = None,
        created_after: datetime | None = None,
        city: str | None = None,
        country: str | None = None,
        person_ids: Iterable[str] | None = None,
        album_ids: Iterable[str] | None = None,
        tag_ids: Iterable[str] | None = None,
        favorites: bool = False,
        min_rating: int | None = None,
    ) -> list[Asset]:
        """POST /search/random — returns array of asset DTOs directly.

        Accepts the same filters as `search_metadata` (city, person, album,
        tag, favourite, minimum rating, upload/capture date). Unlike
        `/search/metadata`, whose ordering is fixed (newest first) and which
        only ever hands back page 1 here, this returns a fresh random sample
        on every call — so selectors that draw a pool per rotation get
        variety instead of the same top-N each time.

        Always sets `withExif: true` and `withPeople: true` — without these
        flags Immich strips exifInfo / people from the response, leaving
        camera, city, country, taken_at and overlay-people fields null.
        """
        body = self._search_body(
            count,
            taken_after=taken_after, created_after=created_after,
            city=city, country=country, person_ids=person_ids,
            album_ids=album_ids, tag_ids=tag_ids,
            favorites=favorites, min_rating=min_rating,
            images_only=not with_video,
        )
        data = self._post("/search/random", json=body)
        if not isinstance(data, list):
            raise ImmichError(f"/search/random: expected list, got {type(data).__name__}")
        return [_to_asset(d) for d in data if showable(d)]

    def search_smart(
        self, query: str, *, count: int = 20, page: int | None = None,
        with_video: bool = True,
    ) -> list[Asset]:
        """POST /search/smart — CLIP search, ranked by similarity.

        Results are deterministic for a query; `page` (1-based, `count` per
        page) lets callers sample beyond the top-N so a repeated label
        doesn't always yield the same photos.
        """
        body = self._search_body(count, images_only=not with_video)
        body["query"] = query
        if page is not None and page > 1:
            body["page"] = int(page)
        data = self._post("/search/smart", json=body)
        return _items_from_search(data)

    def search_statistics(
        self,
        *,
        person_ids: Iterable[str] | None = None,
        city: str | None = None,
        album_ids: Iterable[str] | None = None,
        tag_ids: Iterable[str] | None = None,
        favorites: bool = False,
        min_rating: int | None = None,
        with_video: bool = True,
    ) -> int:
        """POST /search/statistics — number of timeline assets matching the
        filters. Cheap: a count query, no asset payload. Used to size a
        person's library before choosing them for a rotation."""
        body = self._search_body(
            0, person_ids=person_ids, city=city, album_ids=album_ids,
            tag_ids=tag_ids, favorites=favorites, min_rating=min_rating,
            images_only=not with_video,
        )
        body.pop("size", None)
        body.pop("withExif", None)
        body.pop("withPeople", None)
        data = self._post("/search/statistics", json=body)
        if not isinstance(data, dict) or not isinstance(data.get("total"), int):
            raise ImmichError("/search/statistics: expected {total: int}")
        return int(data["total"])

    def search_metadata(
        self,
        *,
        taken_after: datetime | None = None,
        taken_before: datetime | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        city: str | None = None,
        country: str | None = None,
        tag_ids: Iterable[str] | None = None,
        person_ids: Iterable[str] | None = None,
        count: int = 20,
    ) -> list[Asset]:
        """POST /search/metadata with structured filters.

        `taken_*` filters by the photo's EXIF capture time.
        `created_*` filters by upload time to Immich.

        Note: results come back in a fixed order (newest first) and only the
        first page is fetched, so repeated calls with the same filters return
        the same assets. Use `random_assets(...)` with filters when a varied
        sample is what's wanted.
        """
        body = self._search_body(
            count,
            taken_after=taken_after, taken_before=taken_before,
            created_after=created_after, created_before=created_before,
            city=city, country=country, tag_ids=tag_ids, person_ids=person_ids,
        )
        data = self._post("/search/metadata", json=body)
        return _items_from_search(data)

    def _search_body(self, count: int, **filters: Any) -> dict[str, Any]:
        """Request body for the /search/* family in the dialect this server
        speaks: the v3.2+ structured `filter` object, or the flat fields on
        older servers (where `filter` would be silently dropped)."""
        return search_body(count, structured=self.structured_filters, **filters)

    def list_memories(self) -> list[dict[str, Any]]:
        """GET /memories — returns the list of on-this-day memories.

        Each entry has `{id, type, memoryAt, data: {year}, assets: [...]}`.
        Assets in memories DO NOT carry exifInfo by default — the overlay
        will show date / file / people but not city / camera. To get full
        exif, you'd need to re-fetch each asset via `asset()`.
        """
        data = self._get("/memories")
        if not isinstance(data, list):
            raise ImmichError("/memories: expected list")
        return [m for m in data if isinstance(m, dict)]

    def get_ocr(self, asset_id: str) -> list[str]:
        """GET /assets/{id}/ocr — returns the visible text strings found in
        the image by Immich's OCR job, in document order.

        Empty list when OCR hasn't run, the asset has no text, or all
        detected boxes were marked not-visible.
        """
        data = self._get(f"/assets/{asset_id}/ocr")
        if not isinstance(data, list):
            return []
        texts = []
        for box in data:
            if not isinstance(box, dict):
                continue
            if box.get("isVisible") is False:
                continue
            text = box.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
        return texts

    def list_people(
        self, *, include_hidden: bool = False, size: int = 500,
    ) -> list[dict[str, Any]]:
        """GET /people — returns the named-people list (paginated).

        Each entry is a dict with at least `id`, `name`, `isHidden`. Empty
        names mean unnamed face clusters; we leave the filtering to the
        caller so they can decide whether to include them.

        Pages through `hasNextPage` until exhausted (one page if no
        nextPage signal). On libraries with thousands of face clusters
        (mostly unnamed) this can hit the API a few times — sufficient
        for our use, which is one-shot index building at startup.
        """
        params: dict[str, Any] = {"size": size}
        if include_hidden:
            params["withHidden"] = "true"
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            params["page"] = page
            data = self._get("/people", params=params)
            if not isinstance(data, dict):
                raise ImmichError("/people: expected object")
            people = data.get("people") or []
            out.extend(p for p in people if isinstance(p, dict))
            if not data.get("hasNextPage"):
                break
            page += 1
            if page > 50:                                 # safety stop
                break
        return out

    def album_assets(self, album_id: str) -> list[Asset]:
        """GET /albums/{id} — returns AlbumResponseDto with `assets` array."""
        data = self._get(f"/albums/{album_id}")
        if not isinstance(data, dict):
            raise ImmichError(f"/albums/{album_id}: expected object")
        assets = data.get("assets", [])
        return [_to_asset(d) for d in assets if showable(d)]

    def explore(self) -> dict[str, list[str]]:
        """GET /search/explore — return `{field_name: [values]}`.

        Immich's Explore endpoint surfaces auto-discovered groupings. The
        most useful key is "things" (CLIP scene labels — "beach",
        "mountain", "forest", etc.); "people" carries named faces.

        Returns an empty dict / empty lists if Immich hasn't run
        classification on the library yet.
        """
        data = self._get("/search/explore")
        if not isinstance(data, list):
            raise ImmichError("/search/explore: expected list")
        out: dict[str, list[str]] = {}
        for facet in data:
            if not isinstance(facet, dict):
                continue
            name = facet.get("fieldName")
            items = facet.get("items") or []
            if not isinstance(name, str) or not isinstance(items, list):
                continue
            values = [it.get("value") for it in items
                      if isinstance(it, dict) and isinstance(it.get("value"), str) and it.get("value")]
            if values:
                out[name] = values
        return out

    def list_cities(self) -> list[str]:
        """GET /search/cities — every distinct city in the library, sorted.

        Immich answers with one representative asset per city; we only keep
        `exifInfo.city`. Unlike the city facet of `/search/explore`, which is
        capped at 12 entries (alphabetically first — so a big library only
        ever surfaces its "A" cities), this is the full list.
        """
        data = self._get("/search/cities")
        if not isinstance(data, list):
            raise ImmichError("/search/cities: expected list")
        cities: set[str] = set()
        for d in data:
            if not isinstance(d, dict):
                continue
            exif = d.get("exifInfo") or {}
            city = exif.get("city") if isinstance(exif, dict) else None
            if isinstance(city, str) and city.strip():
                cities.add(city.strip())
        return sorted(cities)

    # ── Bytes ───────────────────────────────────────────────────────────
    def download_preview(self, asset_id: str, dest: Path) -> None:
        """Stream the configured-size JPEG to `dest`. Atomic (tmp + rename).

        Uses the `image_size` from the constructor:
        - `preview` (~1440x2560)
        - `fullsize` (original-resolution JPEG; transcoded for HEIC/RAW).
          The /thumbnail endpoint 302-redirects fullsize to
          /assets/{id}/original — `requests` follows redirects by default.

        Self-healing: if `fullsize` returns 401/403 (some Immich servers
        block /original via API-key auth), logs once and permanently
        switches the session to `preview`. No silent retries thereafter.
        """
        url = self._url(f"/assets/{asset_id}/thumbnail")
        try:
            with self._session.get(
                url, params={"size": self._image_size}, stream=True,
                timeout=self._timeout, allow_redirects=True,
            ) as r:
                if r.status_code in (401, 403) and self._image_size == "fullsize":
                    self._fallback_to_preview(asset_id, r.status_code, r.url)
                    return self.download_preview(asset_id, dest)   # retry once at new size
                if r.status_code >= 400:
                    raise ImmichError(
                        f"thumbnail {asset_id}: {r.status_code} "
                        f"(final URL: {r.url})"
                    )
                tmp = dest.with_name(dest.name + ".part")
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp, dest)
        except requests.RequestException as e:
            raise ImmichError(f"thumbnail {asset_id}: {e}") from e

    def _fallback_to_preview(self, asset_id: str, status: int, final_url: str) -> None:
        log.warning(
            "fullsize blocked by server (HTTP %d on %s). Most likely cause: "
            "your Immich API key is missing the 'asset.download' permission "
            "(newer Immich versions are granular). Either: "
            "(1) re-create the API key with download permissions enabled "
            "[Immich -> Account Settings -> API Keys -> edit -> tick "
            "asset.download], or "
            "(2) set `immich.image_size: preview` in your config to use the "
            "smaller (~1440x2560) preview which doesn't need download "
            "permission. "
            "Falling back to 'preview' for the rest of this session.",
            status, final_url,
        )
        self._image_size = "preview"

    @contextmanager
    def stream_preview(self, asset_id: str) -> Iterator[requests.Response]:
        """Yield a streaming `requests.Response` at the configured `image_size`.

        Used by the HTTP control plane to proxy image bytes to clients
        without ever writing to disk. Caller reads via `.iter_content()`
        and may forward `Content-Type` / `Content-Length` headers.

        Falls back to `preview` on 401/403 with `fullsize`, same as
        `download_preview()`.
        """
        url = self._url(f"/assets/{asset_id}/thumbnail")
        try:
            r = self._session.get(
                url,
                params={"size": self._image_size},
                stream=True,
                timeout=self._timeout,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            raise ImmichError(f"thumbnail stream {asset_id}: {e}") from e
        try:
            if r.status_code in (401, 403) and self._image_size == "fullsize":
                self._fallback_to_preview(asset_id, r.status_code, r.url)
                r.close()
                with self.stream_preview(asset_id) as r2:  # retry once at new size
                    yield r2
                return
            if r.status_code >= 400:
                raise ImmichError(
                    f"thumbnail stream {asset_id}: {r.status_code} "
                    f"(final URL: {r.url})"
                )
            yield r
        finally:
            try:
                r.close()
            except Exception:
                pass

    # ── Video (consumed by python-mpv) ──────────────────────────────────
    def video_play_args(self, asset_id: str) -> tuple[str, dict[str, str]]:
        """Returns `(url, headers)` for MPV's `loadfile` + `http-header-fields`."""
        return self._url(f"/assets/{asset_id}/video/playback"), {self.AUTH_HEADER: self._api_key}


# ── JSON → Asset normaliser ─────────────────────────────────────────────
_KIND_MAP = {
    "IMAGE": AssetKind.IMAGE,
    "VIDEO": AssetKind.VIDEO,
    "AUDIO": AssetKind.OTHER,
    "OTHER": AssetKind.OTHER,
}


# Immich 3.2.0 replaced the flat search filter fields (city, personIds,
# takenAfter, visibility, type, …) with one structured `filter` object of
# per-field operators (eq / in / gte / any / …) and marked every flat field
# deprecated. Servers before that ignore `filter`; servers after will one day
# reject the flat fields — so we emit whichever the server understands.
STRUCTURED_FILTER_SINCE: tuple[int, int, int] = (3, 2, 0)


def search_body(
    count: int,
    *,
    structured: bool,
    taken_after: datetime | None = None,
    taken_before: datetime | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    city: str | None = None,
    country: str | None = None,
    tag_ids: Iterable[str] | None = None,
    person_ids: Iterable[str] | None = None,
    album_ids: Iterable[str] | None = None,
    favorites: bool = False,
    min_rating: int | None = None,
    images_only: bool = False,
) -> dict[str, Any]:
    """Body shared by /search/random, /search/metadata, /search/smart and
    /search/statistics (they all take the same filter set).

    Always restricts to timeline assets: without it Immich also returns
    *hidden* assets — chiefly the motion-clip companion of every live /
    motion photo (HEIC + MP4 pairs), which would otherwise be played as
    standalone 3-second videos on top of playing with their still — plus
    archived and locked-folder assets.
    """
    body: dict[str, Any] = {
        "size": count,
        "withExif": True,
        "withPeople": True,
    }
    if structured:
        f: dict[str, Any] = {"visibility": {"eq": "timeline"}}
        taken: dict[str, str] = {}
        if taken_after is not None:
            taken["gte"] = taken_after.isoformat()
        if taken_before is not None:
            taken["lte"] = taken_before.isoformat()
        if taken:
            f["takenAt"] = taken
        created: dict[str, str] = {}
        if created_after is not None:
            created["gte"] = created_after.isoformat()
        if created_before is not None:
            created["lte"] = created_before.isoformat()
        if created:
            f["createdAt"] = created
        if city is not None:
            f["city"] = {"eq": city}
        if country is not None:
            f["country"] = {"eq": country}
        if tag_ids is not None:
            f["tagIds"] = {"any": list(tag_ids)}
        if person_ids is not None:
            f["personIds"] = {"any": list(person_ids)}
        if album_ids is not None:
            f["albumIds"] = {"any": list(album_ids)}
        if favorites:
            f["isFavorite"] = {"eq": True}
        if min_rating is not None:
            f["rating"] = {"gte": int(min_rating)}
        if images_only:
            f["type"] = {"eq": "IMAGE"}
        body["filter"] = f
        return body

    body["visibility"] = "timeline"
    if taken_after is not None:
        body["takenAfter"] = taken_after.isoformat()
    if taken_before is not None:
        body["takenBefore"] = taken_before.isoformat()
    if created_after is not None:
        body["createdAfter"] = created_after.isoformat()
    if created_before is not None:
        body["createdBefore"] = created_before.isoformat()
    if city is not None:
        body["city"] = city
    if country is not None:
        body["country"] = country
    if tag_ids is not None:
        body["tagIds"] = list(tag_ids)
    if person_ids is not None:
        body["personIds"] = list(person_ids)
    if album_ids is not None:
        body["albumIds"] = list(album_ids)
    if favorites:
        body["isFavorite"] = True
    if min_rating is not None:
        # The flat field is an exact match; there is no >= on old servers.
        body["rating"] = int(min_rating)
    if images_only:
        body["type"] = "IMAGE"
    return body


def _items_from_search(data: Any) -> list[Asset]:
    if not isinstance(data, dict):
        raise ImmichError("search response: expected object")
    assets_block = data.get("assets", {})
    items = assets_block.get("items", []) if isinstance(assets_block, dict) else []
    return [_to_asset(d) for d in items if showable(d)]


def showable(d: Any) -> bool:
    """True for an AssetResponseDto dict the frame should display.

    Belt-and-braces beside the `visibility` search filter: endpoints that
    take no filter (albums, memories) and older Immich servers that ignore
    it still get hidden / archived / trashed assets dropped here. Asset
    dicts without a `visibility` field (pre-1.133 servers) pass through.
    """
    if not isinstance(d, dict):
        return False
    vis = d.get("visibility")
    if isinstance(vis, str) and vis != "timeline":
        return False
    if d.get("isArchived") is True or d.get("isTrashed") is True:
        return False
    return True


def _to_asset(d: dict[str, Any]) -> Asset:
    """Map an AssetResponseDto JSON dict to our `Asset` dataclass.

    Defensive: most fields are nullable in Immich. We never raise for
    individual missing values — callers can filter on `kind` if they care.
    """
    exif = d.get("exifInfo") or {}
    tags = d.get("tags") or []
    people_raw = d.get("people") or []
    people = tuple(
        p.get("name") for p in people_raw
        if isinstance(p, dict) and not p.get("isHidden") and p.get("name")
    )
    return Asset(
        id=d["id"],
        kind=_KIND_MAP.get(d.get("type", "OTHER"), AssetKind.OTHER),
        original_file_name=d.get("originalFileName") or "",
        mime_type=d.get("originalMimeType") or "",
        width=int(d.get("width") or 0),
        height=int(d.get("height") or 0),
        taken_at=_parse_dt(d.get("localDateTime") or d.get("fileCreatedAt")),
        geo=GeoInfo(
            latitude=_to_float(exif.get("latitude")),
            longitude=_to_float(exif.get("longitude")),
            city=exif.get("city"),
            state=exif.get("state"),
            country=exif.get("country"),
        ),
        camera_make=exif.get("make"),
        camera_model=exif.get("model"),
        title=None,                                     # Immich has no separate title field
        caption=exif.get("description"),
        tag_names=tuple(t.get("value") or t.get("name") or "" for t in tags),
        people=people,
        favorite=bool(d.get("isFavorite", False)),
        live_photo_video_id=d.get("livePhotoVideoId") or None,
    )


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    # Immich emits ISO 8601 with 'Z' suffix or +HH:MM offset.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        log.debug("could not parse datetime %r", s)
        return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

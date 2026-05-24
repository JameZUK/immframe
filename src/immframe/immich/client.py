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
    PREVIEW_SIZE = "preview"            # AssetMediaSize: original|fullsize|preview|thumbnail
    _API_PREFIX = "/api"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        self._base = base_url.rstrip("/") + self._API_PREFIX
        self._api_key = api_key
        self._timeout = timeout_s
        if session is None:
            self._session = requests.Session()
            self._owns_session = True
        else:
            self._session = session
            self._owns_session = False
        self._session.headers.setdefault(self.AUTH_HEADER, api_key)
        self._session.headers.setdefault("Accept", "application/json")

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

    # ── Asset selection ─────────────────────────────────────────────────
    def random_assets(self, count: int, *, with_video: bool = True) -> list[Asset]:
        """POST /search/random — returns array of asset DTOs directly."""
        body: dict[str, Any] = {"size": count}
        if not with_video:
            body["type"] = "IMAGE"
        data = self._post("/search/random", json=body)
        if not isinstance(data, list):
            raise ImmichError(f"/search/random: expected list, got {type(data).__name__}")
        return [_to_asset(d) for d in data]

    def search_smart(self, query: str, *, count: int = 20) -> list[Asset]:
        """POST /search/smart — returns SearchResponseDto with assets.items."""
        body = {"query": query, "size": count}
        data = self._post("/search/smart", json=body)
        return _items_from_search(data)

    def search_metadata(
        self,
        *,
        taken_after: datetime | None = None,
        taken_before: datetime | None = None,
        city: str | None = None,
        country: str | None = None,
        tag_ids: Iterable[str] | None = None,
        count: int = 20,
    ) -> list[Asset]:
        """POST /search/metadata with structured filters."""
        body: dict[str, Any] = {"size": count}
        if taken_after is not None:
            body["takenAfter"] = taken_after.isoformat()
        if taken_before is not None:
            body["takenBefore"] = taken_before.isoformat()
        if city is not None:
            body["city"] = city
        if country is not None:
            body["country"] = country
        if tag_ids is not None:
            body["tagIds"] = list(tag_ids)
        data = self._post("/search/metadata", json=body)
        return _items_from_search(data)

    def album_assets(self, album_id: str) -> list[Asset]:
        """GET /albums/{id} — returns AlbumResponseDto with `assets` array."""
        data = self._get(f"/albums/{album_id}")
        if not isinstance(data, dict):
            raise ImmichError(f"/albums/{album_id}: expected object")
        assets = data.get("assets", [])
        return [_to_asset(d) for d in assets]

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

    # ── Bytes ───────────────────────────────────────────────────────────
    def download_preview(self, asset_id: str, dest: Path) -> None:
        """Stream the preview JPEG to `dest`. Atomic (tmp file + rename)."""
        url = self._url(f"/assets/{asset_id}/thumbnail")
        try:
            with self._session.get(
                url, params={"size": self.PREVIEW_SIZE}, stream=True, timeout=self._timeout
            ) as r:
                if r.status_code >= 400:
                    raise ImmichError(f"thumbnail {asset_id}: {r.status_code}")
                tmp = dest.with_name(dest.name + ".part")
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)
                os.replace(tmp, dest)
        except requests.RequestException as e:
            raise ImmichError(f"thumbnail {asset_id}: {e}") from e

    @contextmanager
    def stream_preview(self, asset_id: str) -> Iterator[requests.Response]:
        """Yield a streaming `requests.Response` for the preview JPEG.

        Used by the HTTP control plane to proxy image bytes to clients
        without ever writing to disk. Caller reads via `.iter_content()`
        and may forward `Content-Type` / `Content-Length` headers.

        Raises `ImmichError` on any failure.
        """
        url = self._url(f"/assets/{asset_id}/thumbnail")
        try:
            r = self._session.get(
                url,
                params={"size": self.PREVIEW_SIZE},
                stream=True,
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            raise ImmichError(f"thumbnail stream {asset_id}: {e}") from e
        try:
            if r.status_code >= 400:
                raise ImmichError(f"thumbnail stream {asset_id}: {r.status_code}")
            yield r
        finally:
            r.close()

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


def _items_from_search(data: Any) -> list[Asset]:
    if not isinstance(data, dict):
        raise ImmichError("search response: expected object")
    assets_block = data.get("assets", {})
    items = assets_block.get("items", []) if isinstance(assets_block, dict) else []
    return [_to_asset(d) for d in items]


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

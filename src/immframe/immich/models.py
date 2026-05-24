"""Domain types normalised from Immich API responses.

These are the boundary between the network layer (`client.py`) and the rest
of the app. Every other module consumes `Asset` and is unaware of Immich's
JSON shape — if Immich changes its schema, only `client.py` needs to change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class AssetKind(str, Enum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    OTHER = "OTHER"


@dataclass(frozen=True, slots=True)
class GeoInfo:
    latitude: float | None
    longitude: float | None
    city: str | None
    state: str | None
    country: str | None


@dataclass(frozen=True, slots=True)
class Asset:
    """Immich asset, normalised to the shape the viewer expects.

    Frozen so it's safe to share across the prefetch worker / render thread
    boundary without locks.

    `width` and `height` are the DISPLAYED dimensions — Immich's preview
    JPEGs are already pre-rotated, and the metadata reflects the post-
    rotation shape. The viewer should NOT apply any further rotation; just
    render the bytes as-is.
    """

    id: str
    kind: AssetKind
    original_file_name: str
    mime_type: str
    width: int                          # displayed width (preview is pre-rotated)
    height: int                         # displayed height
    taken_at: datetime | None
    geo: GeoInfo
    camera_make: str | None
    camera_model: str | None
    title: str | None
    caption: str | None
    tag_names: tuple[str, ...]
    people: tuple[str, ...]              # named (non-hidden) people from Immich
    favorite: bool

    @property
    def is_portrait(self) -> bool:
        return self.height > self.width

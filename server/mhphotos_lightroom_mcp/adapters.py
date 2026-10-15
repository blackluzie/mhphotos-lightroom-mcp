"""Catalog adapters: access to a (mock or real) Lightroom catalog.

Reads are always available. Writes and exports exist on the interface but are
only reachable through the permission gate in ``permissions.py``.
"""

from __future__ import annotations

import base64
import dataclasses
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# Optional metadata the plugin may report (exposure, lens, place, GPS).
EXTRA_KEYS = ("iso", "aperture", "shutter_speed", "focal_length_mm", "lens", "location", "gps")
MAX_PAGE_SIZE = 100
MAX_BATCH = 100
FLAGS = ("none", "picked", "rejected")


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class PhotoFilter:
    """All set criteria must match. Dates are YYYY-MM-DD (inclusive)."""

    min_rating: int | None = None
    captured_from: str | None = None
    captured_to: str | None = None
    keyword: str | None = None
    flag: str | None = None
    location: str | None = None  # substring of the place text (city, region, country)
    near_lat: float | None = None
    near_lon: float | None = None
    radius_km: float | None = None

    def to_args(self) -> dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}

    def matches(self, photo: Photo) -> bool:
        if self.min_rating is not None and photo.rating < self.min_rating:
            return False
        day = photo.capture_time[:10]
        if self.captured_from is not None and (not day or day < self.captured_from):
            return False
        if self.captured_to is not None and (not day or day > self.captured_to):
            return False
        if self.keyword is not None and self.keyword.lower() not in {
            k.lower() for k in photo.keywords
        }:
            return False
        if self.flag is not None and photo.flag != self.flag:
            return False
        if self.location is not None and self.location.lower() not in str(
            photo.extra.get("location", "")
        ).lower():
            return False
        if self.near_lat is not None:
            gps = photo.extra.get("gps")
            if not gps:
                return False
            if distance_km(self.near_lat, self.near_lon, gps["latitude"], gps["longitude"]) > self.radius_km:
                return False
        return True


class NotFoundError(LookupError):
    """Requested collection or photo does not exist."""


@dataclass(frozen=True)
class Collection:
    id: str
    name: str
    photo_ids: tuple[str, ...] = ()
    photo_count: int | None = None

    def summary(self) -> dict[str, Any]:
        count = self.photo_count if self.photo_count is not None else len(self.photo_ids)
        return {"id": self.id, "name": self.name, "photo_count": count}


@dataclass(frozen=True)
class Photo:
    id: str
    filename: str
    rating: int = 0
    flag: str = "none"  # none | picked | rejected
    keywords: tuple[str, ...] = ()
    capture_time: str = ""
    camera: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "filename": self.filename, "rating": self.rating}

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "rating": self.rating,
            "flag": self.flag,
            "keywords": list(self.keywords),
            "capture_time": self.capture_time,
            "camera": self.camera,
            **self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Photo:
        extra = {k: d[k] for k in EXTRA_KEYS if d.get(k) not in (None, "")}
        return cls(
            id=str(d["id"]),
            filename=str(d.get("filename", "")),
            rating=int(d.get("rating") or 0),
            flag=str(d.get("flag") or "none"),
            keywords=tuple(str(k) for k in d.get("keywords") or ()),
            capture_time=str(d.get("capture_time") or ""),
            camera=str(d.get("camera") or ""),
            extra=extra,
        )


class CatalogAdapter(Protocol):
    name: str

    def is_connected(self) -> bool: ...

    # --- reads ---
    def list_collections(self) -> list[Collection]: ...

    def list_photos(
        self,
        collection_id: str | None,
        limit: int,
        offset: int,
        filters: PhotoFilter | None = None,
    ) -> tuple[list[Photo], int]:
        """Return (page, total). collection_id=None means the whole catalog."""

    def get_photo(self, photo_id: str) -> Photo: ...

    # --- reads for editing advice ---
    def get_preview(self, photo_id: str, size: int) -> bytes:
        """JPEG bytes of a preview, longest edge about ``size`` px."""

    def get_develop_settings(self, photo_id: str) -> dict[str, Any]: ...

    def list_presets(self) -> list[dict[str, str]]:
        """[{"folder": ..., "name": ...}]"""

    # --- gated writes (callers must pass the permission gate first) ---
    def set_rating(self, photo_ids: list[str], rating: int) -> dict[str, Any]: ...

    def set_flag(self, photo_ids: list[str], flag: str) -> dict[str, Any]: ...

    def add_keywords(self, photo_ids: list[str], keywords: list[str]) -> dict[str, Any]: ...

    def export_photos(self, photo_ids: list[str], dest_dir: str) -> dict[str, Any]: ...

    def apply_preset(self, photo_ids: list[str], preset: str) -> dict[str, Any]: ...

    def apply_develop_settings(
        self, photo_ids: list[str], settings: dict[str, Any]
    ) -> dict[str, Any]: ...

    def create_preset(self, name: str, settings: dict[str, Any]) -> dict[str, Any]: ...


# 1x1 placeholder JPEG for the mock (not a real preview).
_MOCK_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)


class MockAdapter:
    """In-memory catalog with sample data. Export writes tiny placeholder files only."""

    name = "mock"

    def __init__(self) -> None:
        photos = [
            Photo("p1", "IMG_0001.dng", 5, "picked", ("portrait", "studio"),
                  "2026-05-01T10:15:00", "Canon EOS R5"),
            Photo("p2", "IMG_0002.dng", 3, "none", ("portrait",),
                  "2026-05-01T10:17:30", "Canon EOS R5"),
            Photo("p3", "IMG_0003.dng", 1, "rejected", (),
                  "2026-05-02T08:00:00", "Canon EOS R5"),
            Photo("p4", "DSC_0100.nef", 4, "none", ("landscape", "sunrise"),
                  "2025-06-12T05:42:10", "Nikon Z8"),
            Photo("p5", "DSC_0101.nef", 0, "none", ("landscape",),
                  "2025-06-12T05:43:00", "Nikon Z8"),
        ]
        self._photos = {p.id: p for p in photos}
        # Südtirol-like sample places so location/GPS searches can be tested.
        for pid, loc, lat, lon in (
            ("p4", "Seiser Alm, Südtirol, Italien", 46.54, 11.62),
            ("p5", "Brixen, Südtirol, Italien", 46.72, 11.66),
            ("p1", "Münster, Nordrhein-Westfalen, Deutschland", 51.96, 7.63),
        ):
            p = self._photos[pid]
            self._photos[pid] = dataclasses.replace(
                p,
                extra={
                    "location": loc,
                    "gps": {"latitude": lat, "longitude": lon},
                    "iso": 100,
                    "aperture": 8.0,
                    "shutter_speed": "1/250",
                    "focal_length_mm": 35,
                    "lens": "24-70mm F2.8",
                },
            )
        self._develop: dict[str, dict[str, Any]] = {
            pid: {"ProcessVersion": "15.4", "Exposure2012": 0.0, "Contrast2012": 0}
            for pid in self._photos
        }
        self._presets = [
            {"folder": "User Presets", "name": "Südtirol Warm"},
            {"folder": "User Presets", "name": "Landschaft Klar"},
            {"folder": "Adobe Color", "name": "Vivid"},
        ]
        self._collections = [
            Collection("c1", "Portraits", ("p1", "p2", "p3")),
            Collection("c2", "Landschaft", ("p4", "p5")),
            Collection("c3", "Leer", ()),
        ]

    def is_connected(self) -> bool:
        return True

    def list_collections(self) -> list[Collection]:
        return list(self._collections)

    def list_photos(self, collection_id, limit, offset, filters=None):
        if collection_id is None:
            ids = list(self._photos)
        else:
            coll = next((c for c in self._collections if c.id == collection_id), None)
            if coll is None:
                raise NotFoundError(f"Collection not found: {collection_id}")
            ids = list(coll.photo_ids)
        photos = [self._photos[i] for i in ids]
        if filters is not None:
            photos = [p for p in photos if filters.matches(p)]
        return photos[offset : offset + limit], len(photos)

    def get_photo(self, photo_id: str) -> Photo:
        try:
            return self._photos[photo_id]
        except KeyError:
            raise NotFoundError(f"Photo not found: {photo_id}") from None

    def get_preview(self, photo_id: str, size: int) -> bytes:
        self.get_photo(photo_id)
        return _MOCK_JPEG

    def get_develop_settings(self, photo_id: str) -> dict[str, Any]:
        self.get_photo(photo_id)
        return dict(self._develop[photo_id])

    def list_presets(self) -> list[dict[str, str]]:
        return list(self._presets)

    def apply_preset(self, photo_ids, preset):
        matches = [
            p for p in self._presets if preset in (p["name"], f"{p['folder']}/{p['name']}")
        ]
        if not matches:
            raise NotFoundError(f"Preset not found: {preset}")
        if len(matches) > 1:
            raise NotFoundError(f"Preset name is ambiguous, use Folder/Name: {preset}")
        photos = self._all(photo_ids)
        for p in photos:
            self._develop[p.id]["_preset"] = preset
        return {"updated": len(photos)}

    def apply_develop_settings(self, photo_ids, settings):
        photos = self._all(photo_ids)
        for p in photos:
            self._develop[p.id].update(settings)
        return {"updated": len(photos)}

    def create_preset(self, name, settings):
        self._presets.append({"folder": "MHPhotos MCP Bridge", "name": name})
        return {"created": name, "folder": "MHPhotos MCP Bridge", "parameters": len(settings)}

    def _all(self, photo_ids: list[str]) -> list[Photo]:
        return [self.get_photo(i) for i in photo_ids]  # raises before any change

    def _update(self, photo_ids: list[str], **changes: Any) -> dict[str, Any]:
        for p in self._all(photo_ids):
            self._photos[p.id] = dataclasses.replace(p, **changes)
        return {"updated": len(photo_ids)}

    def set_rating(self, photo_ids, rating):
        return self._update(photo_ids, rating=rating)

    def set_flag(self, photo_ids, flag):
        return self._update(photo_ids, flag=flag)

    def add_keywords(self, photo_ids, keywords):
        for p in self._all(photo_ids):
            merged = tuple(dict.fromkeys((*p.keywords, *keywords)))
            self._photos[p.id] = dataclasses.replace(p, keywords=merged)
        return {"updated": len(photo_ids)}

    def export_photos(self, photo_ids, dest_dir):
        photos = self._all(photo_ids)
        names = [p.filename.rsplit(".", 1)[0] + ".jpg" for p in photos]
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        for name in names:  # tiny placeholder file so the whole pipeline is testable
            (dest / name).write_bytes(b"\xff\xd8\xff\xd9")
        return {"exported": names, "destination": dest_dir, "mock": True}

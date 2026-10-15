"""Catalog adapters: access to a (mock or real) Lightroom catalog.

Reads are always available. Writes and exports exist on the interface but are
only reachable through the permission gate in ``permissions.py``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

MAX_PAGE_SIZE = 100
MAX_BATCH = 100
FLAGS = ("none", "picked", "rejected")


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
        return cls(
            id=str(d["id"]),
            filename=str(d.get("filename", "")),
            rating=int(d.get("rating") or 0),
            flag=str(d.get("flag") or "none"),
            keywords=tuple(str(k) for k in d.get("keywords") or ()),
            capture_time=str(d.get("capture_time") or ""),
            camera=str(d.get("camera") or ""),
        )


class CatalogAdapter(Protocol):
    name: str

    def is_connected(self) -> bool: ...

    # --- reads ---
    def list_collections(self) -> list[Collection]: ...

    def list_photos(
        self, collection_id: str | None, limit: int, offset: int
    ) -> tuple[list[Photo], int]:
        """Return (page, total). collection_id=None means the whole catalog."""

    def get_photo(self, photo_id: str) -> Photo: ...

    # --- gated writes (callers must pass the permission gate first) ---
    def set_rating(self, photo_ids: list[str], rating: int) -> dict[str, Any]: ...

    def set_flag(self, photo_ids: list[str], flag: str) -> dict[str, Any]: ...

    def add_keywords(self, photo_ids: list[str], keywords: list[str]) -> dict[str, Any]: ...

    def export_photos(self, photo_ids: list[str], dest_dir: str) -> dict[str, Any]: ...


class MockAdapter:
    """In-memory catalog with sample data. Export writes tiny placeholder files only."""

    name = "mock"

    def __init__(self) -> None:
        photos = [
            Photo("p1", "IMG_0001.dng", 5, "picked", ("portrait", "studio"),
                  "2025-05-01T10:15:00", "Canon EOS R5"),
            Photo("p2", "IMG_0002.dng", 3, "none", ("portrait",),
                  "2025-05-01T10:17:30", "Canon EOS R5"),
            Photo("p3", "IMG_0003.dng", 1, "rejected", (),
                  "2025-05-02T08:00:00", "Canon EOS R5"),
            Photo("p4", "DSC_0100.nef", 4, "none", ("landscape", "sunrise"),
                  "2025-06-12T05:42:10", "Nikon Z8"),
            Photo("p5", "DSC_0101.nef", 0, "none", ("landscape",),
                  "2025-06-12T05:43:00", "Nikon Z8"),
        ]
        self._photos = {p.id: p for p in photos}
        self._collections = [
            Collection("c1", "Portraits", ("p1", "p2", "p3")),
            Collection("c2", "Landschaft", ("p4", "p5")),
            Collection("c3", "Leer", ()),
        ]

    def is_connected(self) -> bool:
        return True

    def list_collections(self) -> list[Collection]:
        return list(self._collections)

    def list_photos(self, collection_id, limit, offset):
        if collection_id is None:
            ids = list(self._photos)
        else:
            coll = next((c for c in self._collections if c.id == collection_id), None)
            if coll is None:
                raise NotFoundError(f"Collection not found: {collection_id}")
            ids = list(coll.photo_ids)
        return [self._photos[i] for i in ids[offset : offset + limit]], len(ids)

    def get_photo(self, photo_id: str) -> Photo:
        try:
            return self._photos[photo_id]
        except KeyError:
            raise NotFoundError(f"Photo not found: {photo_id}") from None

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

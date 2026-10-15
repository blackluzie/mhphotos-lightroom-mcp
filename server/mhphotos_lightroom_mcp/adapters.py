"""Catalog adapters: read-only access to a (mock or real) Lightroom catalog."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

MAX_PAGE_SIZE = 100


class NotFoundError(LookupError):
    """Requested collection or photo does not exist."""


@dataclass(frozen=True)
class Collection:
    id: str
    name: str
    photo_ids: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "photo_count": len(self.photo_ids)}


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


class CatalogAdapter(Protocol):
    """Read-only catalog interface. Implementations must never write."""

    name: str

    def is_connected(self) -> bool: ...

    def list_collections(self) -> list[Collection]: ...

    def list_photos(
        self, collection_id: str | None, limit: int, offset: int
    ) -> tuple[list[Photo], int]:
        """Return (page, total). collection_id=None means the whole catalog."""

    def get_photo(self, photo_id: str) -> Photo: ...


class MockAdapter:
    """In-memory catalog with fixed sample data. Never touches real files."""

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

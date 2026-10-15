"""MCP server exposing read-only catalog tools via a pluggable adapter.

Only the mock adapter exists so far: no connection to a real Lightroom catalog.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from .adapters import MAX_PAGE_SIZE, CatalogAdapter, MockAdapter, NotFoundError

mcp = FastMCP("MHPhotos Lightroom MCP")

_adapter: CatalogAdapter = MockAdapter()


def set_adapter(adapter: CatalogAdapter) -> None:
    """Replace the active adapter (used by tests and future bridge setup)."""
    global _adapter
    _adapter = adapter


def _clamp(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, MAX_PAGE_SIZE)), max(0, offset)


@mcp.tool()
def connection_status() -> dict[str, Any]:
    """Report whether a catalog adapter is connected and which mode it runs in."""
    mock = _adapter.name == "mock"
    return {
        "connected": _adapter.is_connected(),
        "mode": "mock" if mock else _adapter.name,
        "message": (
            "Mock adapter with sample data. No Lightroom catalog access."
            if mock
            else f"Connected via adapter '{_adapter.name}' (read-only)."
        ),
    }


@mcp.tool()
def list_collections() -> list[dict[str, Any]]:
    """List all collections with id, name and photo count."""
    return [c.summary() for c in _adapter.list_collections()]


@mcp.tool()
def list_photos(
    collection_id: str | None = None, limit: int = 25, offset: int = 0
) -> dict[str, Any]:
    """List photos (id, filename, rating), optionally of one collection.

    Paginated: limit is capped at 100. Returns total so callers can page on.
    """
    limit, offset = _clamp(limit, offset)
    try:
        photos, total = _adapter.list_photos(collection_id, limit, offset)
    except NotFoundError as exc:
        return {"error": str(exc)}
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "photos": [p.summary() for p in photos],
    }


@mcp.tool()
def get_photo_metadata(photo_id: str) -> dict[str, Any]:
    """Get metadata (rating, flag, keywords, capture time, camera) for one photo."""
    try:
        return _adapter.get_photo(photo_id).metadata()
    except NotFoundError as exc:
        return {"error": str(exc)}


def main() -> None:
    """Run MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

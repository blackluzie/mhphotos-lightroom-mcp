"""MCP server: read tools always on, edit/export tools behind a permission gate.

Adapter is chosen with MHPHOTOS_ADAPTER=mock (default) | bridge.
"""

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

from . import permissions
from .adapters import (
    FLAGS,
    MAX_BATCH,
    MAX_PAGE_SIZE,
    CatalogAdapter,
    MockAdapter,
    NotFoundError,
)
from .bridge import (
    Bridge,
    BridgeAdapter,
    BridgeError,
    BridgeUnavailable,
    write_plugin_config,
)

mcp = FastMCP("MHPhotos Lightroom MCP")

_adapter: CatalogAdapter = MockAdapter()


def set_adapter(adapter: CatalogAdapter) -> None:
    """Replace the active adapter (tests, bridge setup)."""
    global _adapter
    _adapter = adapter


def _clamp(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, MAX_PAGE_SIZE)), max(0, offset)


async def _run(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking adapter call off the event loop; map failures to error results."""
    try:
        return await anyio.to_thread.run_sync(fn, *args)
    except NotFoundError as exc:
        return {"error": str(exc)}
    except BridgeUnavailable as exc:
        return {"error": str(exc), "code": "unavailable"}
    except BridgeError as exc:
        return {"error": f"Lightroom reported: {exc}", "code": "plugin_error"}


def _is_error(result: Any) -> bool:
    return isinstance(result, dict) and "error" in result


# ---------------------------------------------------------------- reads
@mcp.tool()
async def connection_status() -> dict[str, Any]:
    """Report adapter mode, connection state and which write permissions are enabled."""
    mode = _adapter.name
    connected = await anyio.to_thread.run_sync(_adapter.is_connected)
    if mode == "mock":
        message = "Mock adapter with sample data. No Lightroom catalog access."
    elif connected:
        message = "Lightroom Classic plugin connected."
    else:
        message = "Waiting for the Lightroom Classic plugin (is Lightroom running?)."
    return {
        "connected": connected,
        "mode": mode,
        "message": message,
        "edits_enabled": permissions.writes_enabled(),
        "export_enabled": permissions.export_dir() is not None,
    }


@mcp.tool()
async def list_collections() -> dict[str, Any]:
    """List all collections with id, name and photo count."""
    result = await _run(_adapter.list_collections)
    return result if _is_error(result) else {"collections": [c.summary() for c in result]}


@mcp.tool()
async def list_photos(
    collection_id: str | None = None, limit: int = 25, offset: int = 0
) -> dict[str, Any]:
    """List photos (id, filename, rating), optionally of one collection.

    Paginated: limit is capped at 100. Returns total so callers can page on.
    """
    limit, offset = _clamp(limit, offset)
    result = await _run(_adapter.list_photos, collection_id, limit, offset)
    if _is_error(result):
        return result
    photos, total = result
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "photos": [p.summary() for p in photos],
    }


@mcp.tool()
async def get_photo_metadata(photo_id: str) -> dict[str, Any]:
    """Get metadata (rating, flag, keywords, capture time, camera) for one photo."""
    result = await _run(_adapter.get_photo, photo_id)
    return result if _is_error(result) else result.metadata()


# ---------------------------------------------------------------- gated writes
def _check_ids(photo_ids: list[str]) -> dict[str, Any] | None:
    if not photo_ids:
        return {"error": "photo_ids must not be empty"}
    if len(photo_ids) > MAX_BATCH:
        return {"error": f"At most {MAX_BATCH} photos per call"}
    if len(set(photo_ids)) != len(photo_ids):
        return {"error": "photo_ids contains duplicates"}
    return None


async def _edit(
    photo_ids: list[str],
    change: dict[str, Any],
    apply: Callable[[], Any],
    confirm: bool,
) -> dict[str, Any]:
    """Shared edit flow: validate ids -> operator gate -> preview -> confirmed apply."""
    if bad := _check_ids(photo_ids):
        return bad
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    current = []
    for pid in photo_ids:  # also proves every id exists before anything changes
        meta = await _run(_adapter.get_photo, pid)
        if _is_error(meta):
            return meta
        current.append({"id": pid, "filename": meta.filename, "current": meta.metadata()})
    if not confirm:
        return {
            "dry_run": True,
            "change": change,
            "photos": current,
            "hint": "Nothing was changed. Call again with confirm=true to apply.",
        }
    result = await _run(apply)
    return result if _is_error(result) else {"applied": True, "change": change, **result}


@mcp.tool()
async def set_rating(photo_ids: list[str], rating: int, confirm: bool = False) -> dict[str, Any]:
    """Set the star rating (0-5) of photos. Dry run unless confirm=true; needs operator opt-in."""
    if not 0 <= rating <= 5:
        return {"error": "rating must be between 0 and 5"}
    return await _edit(
        photo_ids, {"rating": rating}, lambda: _adapter.set_rating(photo_ids, rating), confirm
    )


@mcp.tool()
async def set_flag(photo_ids: list[str], flag: str, confirm: bool = False) -> dict[str, Any]:
    """Set the pick flag (none|picked|rejected). Dry run unless confirm=true; needs opt-in."""
    if flag not in FLAGS:
        return {"error": f"flag must be one of {', '.join(FLAGS)}"}
    return await _edit(
        photo_ids, {"flag": flag}, lambda: _adapter.set_flag(photo_ids, flag), confirm
    )


@mcp.tool()
async def add_keywords(
    photo_ids: list[str], keywords: list[str], confirm: bool = False
) -> dict[str, Any]:
    """Add keywords to photos (never removes any). Dry run unless confirm=true; needs opt-in."""
    cleaned = [k.strip() for k in keywords if k and k.strip()]
    if not cleaned or len(cleaned) > 20 or any(len(k) > 100 for k in cleaned):
        return {"error": "Provide 1-20 non-empty keywords of at most 100 characters"}
    return await _edit(
        photo_ids,
        {"add_keywords": cleaned},
        lambda: _adapter.add_keywords(photo_ids, cleaned),
        confirm,
    )


@mcp.tool()
async def export_photos(photo_ids: list[str], confirm: bool = False) -> dict[str, Any]:
    """Export photos as JPEG into the operator-configured folder. Dry run unless confirm=true.

    The destination cannot be chosen by the client; it comes from MHPHOTOS_EXPORT_DIR.
    """
    if bad := _check_ids(photo_ids):
        return bad
    dest = permissions.export_dir()
    if dest is None:
        return {"error": permissions.export_disabled_message(), "code": "disabled"}
    names = []
    for pid in photo_ids:
        meta = await _run(_adapter.get_photo, pid)
        if _is_error(meta):
            return meta
        names.append(meta.filename)
    if not confirm:
        return {
            "dry_run": True,
            "destination": dest,
            "files": names,
            "hint": "Nothing was exported. Call again with confirm=true to export.",
        }
    Path(dest).mkdir(parents=True, exist_ok=True)
    result = await _run(_adapter.export_photos, photo_ids, dest)
    return result if _is_error(result) else {"applied": True, **result}


# ---------------------------------------------------------------- startup
def main() -> None:
    """Run MCP server over stdio."""
    kind = os.environ.get("MHPHOTOS_ADAPTER", "mock").lower()
    if kind == "bridge":
        port = int(os.environ.get("MHPHOTOS_BRIDGE_PORT", "47831"))
        bridge = Bridge(token=os.environ.get("MHPHOTOS_BRIDGE_TOKEN"), port=port)
        bridge.start()
        cfg = write_plugin_config(bridge)
        print(f"Bridge listening on {bridge.url}; plugin config: {cfg}", file=sys.stderr)
        set_adapter(BridgeAdapter(bridge))
    elif kind != "mock":
        sys.exit(f"Unknown MHPHOTOS_ADAPTER '{kind}' (use mock or bridge)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

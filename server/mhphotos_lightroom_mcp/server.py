"""MCP server: read tools always on, edit/export tools behind a permission gate.

Adapter is chosen with MHPHOTOS_ADAPTER=mock (default) | bridge.
"""

import bisect
import dataclasses
import json
import os
import random
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import Context, FastMCP, Image

from . import (
    audit,
    catalog_audit,
    catalog_insights,
    develop,
    file_dates,
    gate,
    imaging,
    keyword_check,
    online_marks,
    permissions,
    preset_xmp,
    presort,
    publishing,
    social_crop,
    trip_story,
)
from .adapters import (
    COLOR_LABELS,
    FLAGS,
    MAX_BATCH,
    MAX_PAGE_SIZE,
    CatalogAdapter,
    MockAdapter,
    NotFoundError,
    PhotoFilter,
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
async def lightroom_selftest() -> dict[str, Any]:
    """Read-only check of every Lightroom read command (first photos of the catalog).

    Reports ok/error per command with the exact Lightroom error text. Run this first after
    installing the plugin; nothing is changed.
    """
    result = await _run(_adapter.selftest)
    if _is_error(result):
        return result
    failed = [c for c in result.get("checks", []) if not c.get("ok")]
    return {**result, "all_ok": not failed}


@mcp.tool()
async def list_collections(
    name_contains: str | None = None, include_counts: bool = False
) -> dict[str, Any]:
    """List collections (albums) with id and name.

    name_contains: case-insensitive part of the name, e.g. "09 2026". Collection sets
    appear as "Set/Name". include_counts=true adds photo counts (slow on big catalogs).
    """
    result = await _run(_adapter.list_collections, include_counts)
    if _is_error(result):
        return result
    needle = (name_contains or "").strip().lower()
    return {
        "collections": [
            c.summary(include_counts) for c in result if needle in c.name.lower()
        ]
    }


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _build_filter(
    min_rating: int | None,
    year: int | None,
    captured_from: str | None,
    captured_to: str | None,
    keyword: str | None,
    flag: str | None,
    location: str | None = None,
    near_lat: float | None = None,
    near_lon: float | None = None,
    radius_km: float | None = None,
) -> PhotoFilter | dict[str, Any]:
    """Validate search criteria; returns a PhotoFilter or an error result."""
    if min_rating is not None and not 0 <= min_rating <= 5:
        return {"error": "min_rating must be between 0 and 5"}
    if year is not None:
        if not 1900 <= year <= 2200:
            return {"error": "year must be a four-digit year"}
        if captured_from or captured_to:
            return {"error": "Use either year or captured_from/captured_to, not both"}
        captured_from, captured_to = f"{year}-01-01", f"{year}-12-31"
    for d in (captured_from, captured_to):
        if d is not None and not _DATE.match(d):
            return {"error": "Dates must look like YYYY-MM-DD"}
    if flag is not None and flag not in FLAGS:
        return {"error": f"flag must be one of {', '.join(FLAGS)}"}
    near = (near_lat, near_lon, radius_km)
    if any(v is not None for v in near):
        if any(v is None for v in near):
            return {"error": "near_lat, near_lon and radius_km must be given together"}
        if not (-90 <= near_lat <= 90 and -180 <= near_lon <= 180 and 0 < radius_km <= 500):
            return {"error": "near_lat -90..90, near_lon -180..180, radius_km 0..500"}
    keyword = keyword.strip() if keyword else None
    location = location.strip() if location else None
    return PhotoFilter(
        min_rating=min_rating,
        captured_from=captured_from,
        captured_to=captured_to,
        keyword=keyword or None,
        flag=flag,
        location=location or None,
        near_lat=near_lat,
        near_lon=near_lon,
        radius_km=radius_km,
    )


@mcp.tool()
async def list_photos(
    collection_id: str | None = None,
    limit: int = 25,
    offset: int = 0,
    min_rating: int | None = None,
    year: int | None = None,
    captured_from: str | None = None,
    captured_to: str | None = None,
    keyword: str | None = None,
    flag: str | None = None,
    location: str | None = None,
    near_lat: float | None = None,
    near_lon: float | None = None,
    radius_km: float | None = None,
) -> dict[str, Any]:
    """List/search photos (id, filename, rating), optionally of one collection.

    Filters (all must match): min_rating 0-5, year or captured_from/captured_to
    (YYYY-MM-DD, inclusive), keyword (exact, case-insensitive), flag
    (none|picked|rejected), location (text in city/region/country/place), and
    near_lat + near_lon + radius_km (GPS circle; photos without GPS never match).
    Example: min_rating=5, year=2026. South Tyrol by GPS: near_lat=46.7, near_lon=11.4,
    radius_km=80.
    Paginated: limit is capped at 100. Returns total so callers can page on.
    """
    limit, offset = _clamp(limit, offset)
    filters = _build_filter(
        min_rating, year, captured_from, captured_to, keyword, flag, location, near_lat, near_lon, radius_km
    )
    if isinstance(filters, dict):
        return filters
    result = await _run(
        _adapter.list_photos, collection_id, limit, offset, filters if filters.to_args() else None
    )
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


def _names(items: list[dict[str, Any]], limit: int = 6) -> str:
    names = [i.get("filename") or i.get("id") for i in items]
    more = f" und {len(names) - limit} weitere" if len(names) > limit else ""
    return ", ".join(str(n) for n in names[:limit]) + more


async def _edit(
    photo_ids: list[str],
    change: dict[str, Any],
    apply: Callable[[], Any],
    confirm: bool,
    *,
    ctx: Context | None,
    tool: str,
    summary: str,
    fields: tuple[str, ...] = (),
    undoable: bool = False,
    guard: Callable[[list[dict[str, Any]]], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Shared edit flow: validate ids -> operator gate -> preview -> approval -> apply -> log."""
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
    if guard is not None and (problem := guard(current)):
        return problem
    if not confirm:
        return {
            "dry_run": True,
            "change": change,
            "photos": current,
            "hint": "Nothing was changed. Call again with confirm=true to apply.",
        }
    approved, how = await gate.approve(ctx, f"{summary}\nFotos: {_names(current)}")
    if not approved:
        return gate.refusal(how)
    result = await _run(apply)
    if _is_error(result):
        return result
    before = {c["id"]: {f: c["current"].get(f) for f in fields} for c in current}
    change_id = audit.record(
        tool, photo_ids, change, before=before, undoable=undoable, approved_by=how, result=result
    )
    return {"applied": True, "change": change, "change_id": change_id, **result}


@mcp.tool()
async def set_rating(
    photo_ids: list[str], rating: int, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Set the star rating (0-5) of photos. Dry run unless confirm=true; needs operator opt-in.

    You are asked to approve it when your client supports that. Undoable with undo_last_change.
    """
    if not 0 <= rating <= 5:
        return {"error": "rating must be between 0 and 5"}
    return await _edit(
        photo_ids, {"rating": rating}, lambda: _adapter.set_rating(photo_ids, rating), confirm,
        ctx=ctx, tool="set_rating", fields=("rating",), undoable=True,
        summary=f"Bewertung auf {rating} Sterne setzen",
    )


@mcp.tool()
async def set_flag(
    photo_ids: list[str], flag: str, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Set the pick flag (none|picked|rejected). Dry run unless confirm=true; needs opt-in. Undoable."""
    if flag not in FLAGS:
        return {"error": f"flag must be one of {', '.join(FLAGS)}"}
    return await _edit(
        photo_ids, {"flag": flag}, lambda: _adapter.set_flag(photo_ids, flag), confirm,
        ctx=ctx, tool="set_flag", fields=("flag",), undoable=True,
        summary=f"Markierung auf '{flag}' setzen",
    )


@mcp.tool()
async def add_keywords(
    photo_ids: list[str], keywords: list[str], confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Add keywords to photos (never removes any). Dry run unless confirm=true; needs opt-in. Undoable."""
    cleaned = [k.strip() for k in keywords if k and k.strip()]
    if not cleaned or len(cleaned) > 20 or any(len(k) > 100 for k in cleaned):
        return {"error": "Provide 1-20 non-empty keywords of at most 100 characters"}
    return await _edit(
        photo_ids, {"add_keywords": cleaned}, lambda: _adapter.add_keywords(photo_ids, cleaned),
        confirm, ctx=ctx, tool="add_keywords", fields=("keywords",), undoable=True,
        summary="Keywords hinzufügen: " + ", ".join(cleaned),
    )


@mcp.tool()
async def set_color_label(
    photo_ids: list[str], label: str, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Set the color label (none|red|yellow|green|blue|purple), e.g. to mark picks. Undoable."""
    if label not in COLOR_LABELS:
        return {"error": f"label must be one of {', '.join(COLOR_LABELS)}"}
    return await _edit(
        photo_ids, {"label": label}, lambda: _adapter.set_color_label(photo_ids, label), confirm,
        ctx=ctx, tool="set_color_label", fields=("color_label",), undoable=True,
        summary=f"Farbmarkierung '{label}' setzen",
    )


@mcp.tool()
async def set_title_caption(
    photo_ids: list[str],
    title: str | None = None,
    caption: str | None = None,
    overwrite: bool = False,
    confirm: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Set title and/or caption (IPTC) of photos. Existing text is kept unless overwrite=true.

    Dry run unless confirm=true; needs operator opt-in. Undoable.
    """
    if title is None and caption is None:
        return {"error": "Provide a title and/or a caption"}
    if (title is not None and len(title) > 200) or (caption is not None and len(caption) > 2000):
        return {"error": "title max 200 characters, caption max 2000"}
    change = {k: v for k, v in (("title", title), ("caption", caption)) if v is not None}

    def guard(current: list[dict[str, Any]]) -> dict[str, Any] | None:
        if overwrite:
            return None
        clash = [
            {"id": c["id"], "title": c["current"].get("title"), "caption": c["current"].get("caption")}
            for c in current
            if (title is not None and c["current"].get("title"))
            or (caption is not None and c["current"].get("caption"))
        ]
        if clash:
            return {
                "error": "Some photos already have that text. Pass overwrite=true to replace it.",
                "photos_with_text": clash,
            }
        return None

    return await _edit(
        photo_ids, change, lambda: _adapter.set_title_caption(photo_ids, title, caption), confirm,
        ctx=ctx, tool="set_title_caption", fields=("title", "caption"), undoable=True, guard=guard,
        summary="Titel/Beschreibung setzen: " + " | ".join(f"{k}: {v[:60]}" for k, v in change.items()),
    )


@mcp.tool()
async def create_collection(
    name: str, parent: str | None = None, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Create a collection (album), e.g. for picked photos. Returns the existing one if the name exists.

    parent: a collection-set path like "2026" or "2026/Reisen" (missing sets are created), so the album
    lands under it; omitted = top level. Dry run unless confirm=true; needs operator opt-in.
    Not undoable (delete it in Lightroom).
    """
    name = name.strip()
    if not name or len(name) > 120 or "/" in name:
        return {"error": "name required (max 120 characters, no '/')"}
    parent = _clean_set_path(parent)
    if isinstance(parent, dict):
        return parent
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    existing = await _run(_adapter.list_collections)
    if _is_error(existing):
        return existing
    full = f"{parent}/{name}" if parent else name
    same = [c for c in existing if c.name == full]
    if same:
        return {"exists": True, "id": same[0].id, "name": full, "hint": "Nothing to create; use this id."}
    if not confirm:
        return {"dry_run": True, "would_create": full, "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"Sammlung '{full}' in Lightroom anlegen")
    if not approved:
        return gate.refusal(how)
    result = await _run(_adapter.create_collection, name, parent)
    if _is_error(result):
        return result
    change_id = audit.record("create_collection", [], {"name": full}, approved_by=how, result=result)
    return {"applied": True, "change_id": change_id, **result}


def _clean_set_path(parent: str | None) -> str | None | dict[str, Any]:
    """"2026/ Reisen " -> "2026/Reisen"; None/empty -> None; an error dict for a bad path."""
    if parent is None or not parent.strip():
        return None
    parts = [p.strip() for p in parent.strip().strip("/").split("/")]
    if not parts or any(not p or len(p) > 120 for p in parts) or len(parts) > 6:
        return {"error": "parent must be a path like '2026' or '2026/Reisen' (max 6 levels, 120 characters each)"}
    return "/".join(parts)


@mcp.tool()
async def move_collection(
    collection_id: str, parent: str, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Move an existing collection into a collection set, e.g. "Fotobuch Hoffis 2026" into "2026".

    parent: set path like "2026" or "2026/Reisen" (missing sets are created); empty = top level. The photos
    stay as they are, only the place in the tree changes; move it back the same way. Dry run unless
    confirm=true; needs operator opt-in.
    """
    parent_path = _clean_set_path(parent)
    if isinstance(parent_path, dict):
        return parent_path
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    existing = await _run(_adapter.list_collections)
    if _is_error(existing):
        return existing
    target = next((c for c in existing if c.id == collection_id), None)
    if target is None:
        return {"error": f"Collection not found: {collection_id}. Use list_collections."}
    if not confirm:
        return {"dry_run": True, "would_move": target.name, "into": parent_path or "(top level)",
                "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"Sammlung '{target.name}' nach '{parent_path or 'oberste Ebene'}' verschieben")
    if not approved:
        return gate.refusal(how)
    result = await _run(_adapter.move_collection, collection_id, parent_path or "")
    if _is_error(result):
        return result
    change_id = audit.record("move_collection", [], {"collection": target.name, "parent": parent_path}, approved_by=how, result=result)
    return {"applied": True, "change_id": change_id, **result}


@mcp.tool()
async def create_smart_collection(
    name: str, parent: str | None = None, min_rating: int | None = None, keyword: str | None = None,
    captured_from: str | None = None, captured_to: str | None = None, flag: str | None = None,
    confirm: bool = False, ctx: Context | None = None,
) -> dict[str, Any]:
    """Create a smart collection that fills itself, e.g. "Top 2026": min_rating=4, captured_from=2026-01-01, captured_to=2026-12-31.

    Rules (all must match, at least one needed): min_rating 1-5, keyword, captured_from/captured_to (YYYY-MM-DD),
    flag (picked|rejected). parent: collection-set path like "2026" (created if missing). Dry run unless
    confirm=true; needs operator opt-in. Not undoable (delete it in Lightroom).
    """
    name = name.strip()
    if not name or len(name) > 120 or "/" in name:
        return {"error": "name required (max 120 characters, no '/')"}
    parent = _clean_set_path(parent)
    if isinstance(parent, dict):
        return parent
    rules = _build_filter(min_rating, None, captured_from, captured_to, keyword, flag, None, None, None, None)
    if isinstance(rules, dict):
        return rules
    rule_args = rules.to_args()
    if not rule_args:
        return {"error": "Give at least one rule: min_rating, keyword, captured_from/captured_to or flag"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    full = f"{parent}/{name}" if parent else name
    if not confirm:
        return {"dry_run": True, "would_create": full, "rules": rule_args, "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"Intelligente Sammlung '{full}' anlegen")
    if not approved:
        return gate.refusal(how)
    result = await _run(_adapter.create_smart_collection, name, parent, rule_args)
    if _is_error(result):
        return result
    change_id = audit.record("create_smart_collection", [], {"name": full, "rules": rule_args}, approved_by=how, result=result)
    return {"applied": True, "change_id": change_id, **result}


@mcp.tool()
async def create_keyword(
    name: str, synonyms: list[str] | None = None, include_on_export: bool = True,
    confirm: bool = False, ctx: Context | None = None,
) -> dict[str, Any]:
    """Create a keyword, also as a hierarchy: "Orte > Spanien > Barcelona" (levels are created), with synonyms.

    Photos are not tagged; use add_keywords with the same path for that. Dry run unless confirm=true;
    needs operator opt-in.
    """
    name = name.strip()
    if not name or len(name) > 300:
        return {"error": "name required (max 300 characters); a hierarchy is written 'A > B > C'"}
    syn = [x.strip() for x in (synonyms or []) if x and x.strip()]
    if len(syn) > 20 or any(len(x) > 100 for x in syn):
        return {"error": "at most 20 synonyms of up to 100 characters"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    if not confirm:
        return {"dry_run": True, "would_create": name, "synonyms": syn, "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"Stichwort '{name}' anlegen")
    if not approved:
        return gate.refusal(how)
    result = await _run(_adapter.create_keyword, name, syn, include_on_export)
    if _is_error(result):
        return result
    change_id = audit.record("create_keyword", [], {"name": name, "synonyms": syn}, approved_by=how, result=result)
    return {"applied": True, "change_id": change_id, **result}


@mcp.tool()
async def build_smart_previews(
    photo_ids: list[str], confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Build smart previews (up to 100 photos): editing and contact sheets then also work without the originals (NAS offline).

    Takes time and disk space (about 1-2 MB per photo). Dry run unless confirm=true; needs operator opt-in.
    """
    return await _edit(
        photo_ids, {"smart_previews": len(photo_ids)}, lambda: _adapter.build_smart_previews(photo_ids),
        confirm, ctx=ctx, tool="build_smart_previews",
        summary=f"Smart-Vorschauen für {len(photo_ids)} Fotos erzeugen",
    )


@mcp.tool()
async def import_photo(
    path: str, collection_id: str | None = None, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Add one file to the Lightroom catalog (absolute path on the Mac; the file stays where it is), optionally into a collection.

    Dry run unless confirm=true; needs operator opt-in. Not undoable here (remove it in Lightroom).
    """
    if not path.startswith("/") or ".." in path or len(path) > 1000:
        return {"error": "absolute file path required (no '..')"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    if not confirm:
        return {"dry_run": True, "would_add": path, "collection_id": collection_id, "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"Datei '{path}' in den Katalog aufnehmen")
    if not approved:
        return gate.refusal(how)
    result = await _run(_adapter.import_photo, path, collection_id)
    if _is_error(result):
        return result
    change_id = audit.record("import_photo", [result.get("id", "")], {"path": path}, approved_by=how, result=result)
    return {"applied": True, "change_id": change_id, **result}


CUSTOM_STATUS = ("insta_kandidat", "fotobuch", "veroeffentlicht", "aussortiert")


@mcp.tool()
async def set_custom_metadata(
    photo_ids: list[str], status: str | None = None, note: str | None = None,
    confirm: bool = False, ctx: Context | None = None,
) -> dict[str, Any]:
    """Set the plug-in's own Lightroom fields "MHPhotos-Status" and "MHPhotos-Notiz" (searchable, usable in smart collections).

    status: insta_kandidat | fotobuch | veroeffentlicht | aussortiert, or "" to clear. note: free text (max 1000
    characters), "" clears it. Needs plugin 0.1.17 or newer. Dry run unless confirm=true; needs operator opt-in.
    """
    if status is None and note is None:
        return {"error": "Give status and/or note"}
    if status not in (None, "", *CUSTOM_STATUS):
        return {"error": f"status must be one of: {', '.join(CUSTOM_STATUS)} or empty"}
    if note is not None and len(note) > 1000:
        return {"error": "note must be at most 1000 characters"}
    change = {k: v for k, v in (("mhphotos_status", status), ("mhphotos_note", note)) if v is not None}
    return await _edit(
        photo_ids, change, lambda: _adapter.set_custom_metadata(photo_ids, status, note), confirm,
        ctx=ctx, tool="set_custom_metadata",
        summary="Eigene Felder setzen: " + ", ".join(f"{k}={v[:40]}" for k, v in change.items()),
    )


@mcp.tool()
async def auto_tone(
    photo_ids: list[str], white_balance: bool = False, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Lightroom's own "Auto" tone (and optionally white balance) for up to 10 photos, one after the other.

    Lightroom only does this for the photo shown in the Develop module, so the plugin selects each photo, opens
    Develop and returns to the Library grid at the end; the selection changes and it takes a few seconds per
    photo. A develop step in Lightroom's history (undo there). Needs plugin 0.1.17 or newer. Dry run unless
    confirm=true; needs operator opt-in.
    """
    if len(photo_ids) > 10:
        return {"error": "At most 10 photos per call"}
    return await _edit(
        photo_ids, {"auto_tone": True, "white_balance": white_balance},
        lambda: _adapter.auto_tone(photo_ids, white_balance), confirm, ctx=ctx, tool="auto_tone",
        summary="Auto-Ton" + (" und Auto-Weißabgleich" if white_balance else "") + " anwenden",
    )


@mcp.tool()
async def list_webhooks() -> dict[str, Any]:
    """Names of the webhooks set up on the Mac (for example n8n), usable with send_webhook. Read-only.

    The addresses live only in ~/.mhphotos-lightroom-mcp/webhooks.json and are never shown or chosen here.
    Needs plugin 0.1.18 or newer.
    """
    return await _run(_adapter.list_webhooks)


@mcp.tool()
async def send_webhook(
    name: str, payload: dict[str, Any] | None = None, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Call one of the webhooks from list_webhooks (POST, JSON payload up to 50 KB), e.g. to start an n8n flow.

    Only configured names work, never free addresses. Dry run unless confirm=true; needs operator opt-in.
    The call goes out from Lightroom's plug-in and cannot be taken back. Needs plugin 0.1.18 or newer.
    """
    name = name.strip()
    if not name or len(name) > 40:
        return {"error": "name required (see list_webhooks)"}
    if payload is not None and len(json.dumps(payload)) > 50000:
        return {"error": "payload too large (max 50 KB)"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    known = await _run(_adapter.list_webhooks)
    if _is_error(known):
        return known
    if name not in known.get("hooks", []):
        return {"error": f"Unknown webhook '{name}'. Known: {', '.join(known.get('hooks', [])) or 'none'}. Set them up in webhooks.json."}
    if not confirm:
        return {"dry_run": True, "would_call": name, "payload_keys": sorted((payload or {}).keys()),
                "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"Webhook '{name}' aufrufen")
    if not approved:
        return gate.refusal(how)
    result = await _run(_adapter.send_webhook, name, payload or {})
    if _is_error(result):
        return result
    change_id = audit.record("send_webhook", [], {"name": name, "keys": sorted((payload or {}).keys())}, approved_by=how, result=result)
    return {"applied": True, "change_id": change_id, **result}


@mcp.tool()
async def add_to_collection(
    collection_id: str, photo_ids: list[str], confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Add photos to a (non-smart) collection. Dry run unless confirm=true; needs operator opt-in."""
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    existing = await _run(_adapter.list_collections)
    if _is_error(existing):
        return existing
    target = next((c for c in existing if c.id == collection_id), None)
    if target is None:
        return {"error": f"Collection not found: {collection_id}. Use list_collections."}
    return await _edit(
        photo_ids,
        {"add_to_collection": target.name, "collection_id": collection_id},
        lambda: _adapter.add_to_collection(collection_id, photo_ids),
        confirm, ctx=ctx, tool="add_to_collection",
        summary=f"Zur Sammlung '{target.name}' hinzufügen",
    )


@mcp.tool()
async def create_virtual_copies(
    photo_ids: list[str], copy_name: str | None = None, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Create virtual copies (max 10) so edits can be tried without touching the originals.

    Apply presets/settings to the returned copy ids, compare with get_photo_previews, keep or
    delete the copy in Lightroom. Lightroom only copies the selected photos, so the plugin
    switches to the Library grid and "All Photographs" and changes your selection. copy_name (max 60
    characters) is the name Lightroom shows for the copies, e.g. "Insta 4:5". Lightroom makes all copies of a call in
    one go in the background; this waits about 20 s. state "running" (with job_id) means Lightroom is still at it: ask
    virtual_copy_job(job_id) every 30-60 s. For a few minutes new copies answer photo_busy; just retry later. On
    virtual_copy_timeout or copy_pending let the user make the copy by hand (Cmd+') and go on with that copy's id.
    Needs opt-in.
    """
    if len(photo_ids) > 10:
        return {"error": "At most 10 virtual copies per call"}
    if copy_name is not None and not 0 < len(copy_name.strip()) <= 60:
        return {"error": "copy_name must be 1-60 characters"}
    name = copy_name.strip() if copy_name else None
    return await _edit(
        photo_ids, {"virtual_copies": len(photo_ids), **({"copy_name": name} if name else {})},
        lambda: _adapter.create_virtual_copies(photo_ids, name),
        confirm, ctx=ctx, tool="create_virtual_copies",
        summary="Virtuelle Kopien anlegen (Auswahl in Lightroom ändert sich)",
    )


@mcp.tool()
async def virtual_copy_job(job_id: str) -> dict[str, Any]:
    """Status of a create_virtual_copies (or crop_to_aspect) job that answered state "running": copies made so far
    ({"source_id", "id"}), state running/done/failed/timeout. Read only; answers at once."""
    if not re.fullmatch(r"vc\d{1,9}-\d{1,12}", job_id or ""):
        return {"error": "job_id looks like vc3-1789123456 (from create_virtual_copies)"}
    return await _run(_adapter.virtual_copy_job, job_id)


@mcp.tool()
async def crop_to_aspect(
    photo_ids: list[str], aspect: str = "4:5", focus_x: float = 0.5, focus_y: float = 0.5,
    virtual_copies: bool = True, angle: float = 0.0, confirm: bool = False, ctx: Context | None = None,
) -> dict[str, Any]:
    """Crop up to 10 photos to a social format, as virtual copies by default so the originals stay as they are.

    aspect: 4:5 (Instagram feed, portrait), 9:16 (Stories, Reels), 1:1, 3:4, 2:3, 16:9 or 1.91:1. The crop keeps as
    much of the picture as the format allows, centred on (focus_x, focus_y): 0 = left/top, 1 = right/bottom, 0.5 = middle;
    choose the focus from the picture (the subject), one call per focus. With virtual_copies=true each photo first gets a
    virtual copy named "Insta 4:5" (or the format) and the crop goes on the copy; false crops the photo itself and
    replaces an existing crop. aspect="original" keeps the shape (use it just to level a horizon). angle (degrees, -45..45)
    also straightens the horizon: the crop is then centred and as large as fits without empty corners (focus is ignored);
    the sign is Lightroom's CropAngle, so check the first photo with get_photo_previews and flip the sign if it got worse.
    Check the result with get_photo_previews. If making the virtual copy fails (virtual_copy_timeout, copy_pending), let
    the user make the copy by hand in Lightroom (Cmd+') and crop that copy with virtual_copies=false.
    Dry run unless confirm=true; needs operator opt-in.
    """
    if aspect not in social_crop.ASPECTS and aspect != social_crop.ORIGINAL:
        return {"error": f"aspect must be one of: {', '.join([*social_crop.ASPECTS, social_crop.ORIGINAL])}"}
    if not -45 <= angle <= 45:
        return {"error": "angle must be between -45 and 45 degrees"}
    if len(photo_ids) > 10:
        return {"error": "At most 10 photos per call"}
    if not (0 <= focus_x <= 1 and 0 <= focus_y <= 1):
        return {"error": "focus_x and focus_y must be between 0 and 1"}
    label = "Story" if aspect == "9:16" else "Gerade" if aspect == social_crop.ORIGINAL else "Insta"

    def apply() -> dict[str, Any]:
        targets, copies = list(photo_ids), []
        if virtual_copies:
            made = _adapter.create_virtual_copies(photo_ids, f"{label} {aspect}")
            copies = made.get("copies") or []
            if made.get("state") == "running":
                return {"error": "Lightroom is still making the virtual copies; nothing was cropped yet.",
                        "code": "copies_pending", "job_id": made.get("job_id"), "copies": copies,
                        "hint": f"Ask virtual_copy_job('{made.get('job_id')}') every 30-60 s; when it is done, crop the "
                        "copy ids with crop_to_aspect(..., virtual_copies=false) (retry on photo_busy)."}
            targets = [c["id"] for c in copies]
        try:
            return _crop(targets, copies)
        except BridgeError as exc:
            if not copies or not str(exc).startswith("photo_busy"):
                raise
            return {"error": "The virtual copies were made, but Lightroom is still finishing them; nothing was cropped "
                    "yet.", "code": "photo_busy", "copies": copies,
                    "hint": "In a few minutes crop the copy ids with crop_to_aspect(..., virtual_copies=false)."}

    def _crop(targets: list[str], copies: list[dict[str, Any]]) -> dict[str, Any]:
        _adapter.apply_develop_settings(targets, social_crop.FULL_FRAME)  # a copy inherits the crop of its source
        dims = _adapter.photo_dimensions(targets).get("dimensions") or {}
        cropped, used = [], {}
        for t in targets:
            size = dims.get(t)
            if not size:
                raise NotFoundError(f"Lightroom reported no size for {t}")
            width, height = social_crop.uncropped_size(size)
            settings = social_crop.crop_settings(width, height, aspect, focus_x, focus_y, angle)
            _adapter.apply_develop_settings([t], settings)
            after = (_adapter.photo_dimensions([t]).get("dimensions") or {}).get(t) or {}
            if not social_crop.crop_shape_ok(settings, width, height, after):
                # a picture stored turned (phone portrait): Lightroom reads Crop* in the stored frame
                settings = social_crop.to_raw_orientation(settings, size.get("orientation"))
                _adapter.apply_develop_settings([t], settings)
                after = (_adapter.photo_dimensions([t]).get("dimensions") or {}).get(t) or {}
                expected = social_crop.ASPECTS.get(aspect)
                if expected and abs(after.get("width", 0) / max(after.get("height", 1), 1) - expected[0] / expected[1]) > 0.02 * expected[0] / expected[1] and not angle:
                    raise NotFoundError(f"Lightroom cropped {t} to {after}, not {aspect}; check it in Lightroom")
            cropped.append(t)
            used[t] = {"size": [width, height], "lightroom_size": size, "crop": settings, "result_size": after}
        return {"cropped": len(cropped), "aspect": aspect, "copies": copies or None, "photo_ids": cropped, "used": used}

    return await _edit(
        photo_ids, {"crop_aspect": aspect, "focus": [focus_x, focus_y], "angle": angle, "virtual_copies": virtual_copies}, apply,
        confirm, ctx=ctx, tool="crop_to_aspect",
        summary=f"Zuschnitt {aspect}{' auf virtuellen Kopien' if virtual_copies else ''}",
    )


async def _collect(filters: PhotoFilter, cap: int = 500) -> tuple[list[Any], int] | dict[str, Any]:
    """Page through list_photos (100 per page) up to cap photos. Returns (photos, total)."""
    out: list[Any] = []
    offset = 0
    while True:
        res = await _run(_adapter.list_photos, None, MAX_PAGE_SIZE, offset, filters)
        if _is_error(res):
            return res
        photos, total = res
        out += photos
        offset += MAX_PAGE_SIZE
        if offset >= total or len(out) >= cap:
            return out, total


@mcp.tool()
async def backlog_report(year: int, min_rating: int = 5, sample: int = 30) -> dict[str, Any]:
    """What good photos of a year are not yet handled? Compares Lightroom against markers.

    Candidates: photos of the year rated min_rating or more. A photo counts as handled when it
    carries the Lightroom keyword "MHPhotos-Inbox" (set by send_to_mhphotos_inbox with
    mark_in_lightroom=true) or "Instagram" (set when matched to an Instagram post). Only as
    reliable as those markers. Read-only.
    """
    if not 1900 <= year <= 2200 or not 0 <= min_rating <= 5:
        return {"error": "year must be four digits and min_rating 0-5"}
    base = PhotoFilter(min_rating=min_rating, captured_from=f"{year}-01-01", captured_to=f"{year}-12-31")
    parts = {}
    for name, flt in (
        ("candidates", base),
        ("inbox", dataclasses.replace(base, keyword=INBOX_KEYWORD)),
        ("instagram", dataclasses.replace(base, keyword="Instagram")),
    ):
        res = await _collect(flt)
        if isinstance(res, dict):
            return res
        parts[name] = res
    candidates, total = parts["candidates"]
    handled = {p.id for p in parts["inbox"][0]} | {p.id for p in parts["instagram"][0]}
    open_items = [p for p in candidates if p.id not in handled]
    return {
        "year": year,
        "min_rating": min_rating,
        "candidates": total,
        "sent_to_mhphotos_inbox": parts["inbox"][1],
        "marked_instagram": parts["instagram"][1],
        "not_yet_handled": len(open_items),
        "not_yet_handled_sample": [p.summary() for p in open_items[: max(1, min(sample, 100))]],
        "truncated": total > len(candidates),
        "note": 'Handled means: keyword "MHPhotos-Inbox" or "Instagram" present.',
    }


# ---------------------------------------------------------------- photos by capture date
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RANGE_PAGE = 2000
RANGE_CAP = 60000
MISSING = ("caption", "title", "keywords", "gps", "place")


def _check_range(captured_from: str, captured_to: str) -> dict[str, Any] | None:
    if not DATE_RE.match(captured_from or "") or not DATE_RE.match(captured_to or ""):
        return {"error": "captured_from and captured_to must be YYYY-MM-DD"}
    if captured_from > captured_to:
        return {"error": "captured_from must not be after captured_to"}
    return None


async def _range(captured_from: str, captured_to: str, refresh: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    """Every photo captured in the range (virtual copies excluded), oldest first."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        res = await _run(_adapter.photos_by_date, captured_from, captured_to, offset, RANGE_PAGE, refresh and offset == 0)
        if _is_error(res):
            return res
        out += [r for r in res.get("photos") or [] if r.get("id") and not r.get("vc")]
        nxt, total = int(res["next_offset"]), int(res["total"])
        if nxt <= offset or nxt >= total or len(out) >= RANGE_CAP:
            return out
        offset = nxt


def _brief(r: dict[str, Any]) -> dict[str, Any]:
    out = {"id": r["id"], "file": r.get("file"), "captured": r.get("t"), "rating": r.get("r") or 0,
           "flag": {1: "picked", -1: "rejected"}.get(r.get("pick"), "none")}
    for key in ("place", "cam"):
        if r.get(key):
            out[key] = r[key]
    out.update({k: True for k in ("title", "caption", "insta") if r.get(k)})
    if r.get("gps"):
        out["gps"] = True
    return out


@mcp.tool()
async def photos_by_date(
    captured_from: str, captured_to: str, unreviewed: bool = False, min_rating: int | None = None,
    max_rating: int | None = None, flag: str | None = None, missing: str | None = None,
    exclude_instagram: bool = False, refresh: bool = False, limit: int = 100, offset: int = 0,
) -> dict[str, Any]:
    """Photos captured between two days (YYYY-MM-DD, inclusive), fast even on a 250k catalog.

    For sorting a new shoot (unreviewed=true: no stars and no flag), best-of picks (min_rating=4),
    photos lacking text (missing=caption|title|keywords) or a place (missing=gps|place), and
    Instagram ideas (exclude_instagram=true skips photos with the keyword "Instagram").
    The first call builds a capture-date index (can take a minute); after a new import pass
    refresh=true. counts summarises the whole range. Read-only.
    """
    if bad := _check_range(captured_from, captured_to):
        return bad
    if missing is not None and missing not in MISSING:
        return {"error": f"missing must be one of: {', '.join(MISSING)}"}
    if flag is not None and flag not in FLAGS:
        return {"error": f"flag must be one of: {', '.join(FLAGS)}"}
    recs = await _range(captured_from, captured_to, refresh)
    if isinstance(recs, dict):
        return recs

    def keep(r: dict[str, Any]) -> bool:
        rating = r.get("r") or 0
        if unreviewed and (rating or r.get("pick")):
            return False
        if min_rating is not None and rating < min_rating:
            return False
        if max_rating is not None and rating > max_rating:
            return False
        if flag is not None and {1: "picked", -1: "rejected"}.get(r.get("pick"), "none") != flag:
            return False
        if exclude_instagram and r.get("insta"):
            return False
        key = {"keywords": "kw"}.get(missing or "", missing)
        return not (missing and r.get(key))

    hits = [r for r in recs if keep(r)]
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    page = hits[offset : offset + limit]
    counts = {
        "in_range": len(recs), "matching": len(hits),
        "unreviewed": sum(1 for r in recs if not r.get("r") and not r.get("pick")),
        "rated_4_plus": sum(1 for r in recs if (r.get("r") or 0) >= 4),
        "rejected": sum(1 for r in recs if r.get("pick") == -1),
        "without_caption": sum(1 for r in recs if not r.get("caption")),
        "without_gps": sum(1 for r in recs if not r.get("gps")),
    }
    out = {"counts": counts, "photos": [_brief(r) for r in page]}
    if offset + len(page) < len(hits):
        out["next_offset"] = offset + len(page)
    if len(recs) >= RANGE_CAP:
        out["truncated"] = f"only the first {RANGE_CAP} photos of the range were read; narrow the dates"
    return out


@mcp.tool()
async def suggest_locations(
    captured_from: str, captured_to: str, max_minutes: int = 30, clock_offset_minutes: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """Suggest places for photos without GPS from photos with GPS taken around the same time.

    Typical case: camera photos without GPS, iPhone photos with GPS on the same walk. For each photo
    without GPS the nearest photo with GPS within max_minutes is used. clock_offset_minutes corrects
    a camera clock that differs from the phone (e.g. +60 when the camera still shows home time while
    abroad; check one known pair first). Suggestions are grouped by position, so each group can be
    written with one set_location call. Read-only.
    """
    if bad := _check_range(captured_from, captured_to):
        return bad
    if not 1 <= max_minutes <= 240 or abs(clock_offset_minutes) > 24 * 60:
        return {"error": "max_minutes 1-240, clock_offset_minutes within one day"}
    recs = await _range(captured_from, captured_to)
    if isinstance(recs, dict):
        return recs

    def when(r: dict[str, Any]) -> datetime | None:
        try:
            return datetime.fromisoformat(str(r.get("t"))[:19])
        except ValueError:
            return None

    anchors = sorted(((when(r), r) for r in recs if r.get("gps") and when(r)), key=lambda a: a[0])
    times = [a[0] for a in anchors]
    groups: dict[tuple, dict[str, Any]] = {}
    without = 0
    for r in recs:
        if r.get("gps"):
            continue
        without += 1
        t = when(r)
        if t is None or not anchors:
            continue
        t = t + timedelta(minutes=clock_offset_minutes)
        i = bisect.bisect_left(times, t)
        near = [anchors[j] for j in (i - 1, i) if 0 <= j < len(anchors)]
        best_t, best = min(near, key=lambda a: abs((a[0] - t).total_seconds()))
        minutes = abs((best_t - t).total_seconds()) / 60
        if minutes > max_minutes:
            continue
        gps = best["gps"]
        key = (round(gps["latitude"], 4), round(gps["longitude"], 4), best.get("place") or "")
        g = groups.setdefault(key, {"latitude": gps["latitude"], "longitude": gps["longitude"],
                                    "place": best.get("place"), "from_photo": best.get("file"),
                                    "photo_ids": [], "files": [], "max_minutes_apart": 0})
        g["photo_ids"].append(r["id"])
        g["files"].append(r.get("file"))
        g["max_minutes_apart"] = max(g["max_minutes_apart"], round(minutes))
    ranked = sorted(groups.values(), key=lambda g: -len(g["photo_ids"]))
    for g in ranked:
        g["files"] = g["files"][:8]
    return {"photos_without_gps": without, "photos_with_suggestion": sum(len(g["photo_ids"]) for g in ranked),
            "groups": ranked[: max(1, min(limit, 200))],
            "hint": "Check one group with get_photo_previews, then set_location(photo_ids, latitude, longitude, ...)."}


@mcp.tool()
async def set_location(
    photo_ids: list[str], latitude: float | None = None, longitude: float | None = None,
    location: str | None = None, city: str | None = None, state: str | None = None,
    country: str | None = None, confirm: bool = False, ctx: Context | None = None,
) -> dict[str, Any]:
    """Set GPS position and/or place names (Ort, Stadt, Bundesland, Land) on up to 100 photos.

    GPS is written where this Lightroom version allows it (the result says gps_written); the
    place names are written either way. Overwrites existing values; not undoable here (Lightroom's
    metadata history keeps the old values). Dry run unless confirm=true; needs operator opt-in.
    """
    if (latitude is None) != (longitude is None):
        return {"error": "latitude and longitude go together"}
    if latitude is not None and not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return {"error": "latitude/longitude out of range"}
    place = {k: v.strip() for k, v in (("location", location), ("city", city), ("stateProvince", state),
                                        ("country", country)) if v and v.strip()}
    if latitude is None and not place:
        return {"error": "give latitude/longitude and/or a place name"}
    if any(len(v) > 200 for v in place.values()):
        return {"error": "place names max 200 characters"}
    change = {"set_location": {**({"latitude": latitude, "longitude": longitude} if latitude is not None else {}), **place}}
    return await _edit(
        photo_ids, change, lambda: _adapter.set_location(photo_ids, latitude, longitude, place),
        confirm, ctx=ctx, tool="set_location", fields=("gps", "location"),
        summary=f"Ort setzen: {', '.join(place.values()) or ''} {f'({latitude:.5f}, {longitude:.5f})' if latitude is not None else ''}",
    )


_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
MAX_FILE_BATCH = 50


def _valid_time(text: str) -> bool:
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return bool(_TIME.match(text)) and 1900 <= int(text[:4]) <= 2100


def _name_date_items(saved: dict[str, Any], year: int | None, only_wrong_folder: bool) -> tuple[list[dict[str, Any]], int]:
    """date_in_name findings that carry a full time, and how many carry only a date."""
    found = saved["findings"].get("date_in_name", [])
    if year is not None:
        found = [f for f in found if str(f.get("name_date") or "")[:4] == str(year)]
    if only_wrong_folder:
        found = [f for f in found if not f.get("folder_fits")]
    # the saved name_date must agree with today's rule (older checks also took dates from brackets)
    found = [f for f in found if catalog_audit.name_date(str(f.get("file") or "")) == f.get("name_date")]
    timed = [f for f in found if _valid_time(str(f.get("name_date") or ""))]
    return timed, len(found) - len(timed)


@mcp.tool()
async def write_name_dates_to_files(
    year: int | None = None, only_wrong_folder: bool = False, limit: int = 1,
    confirm: bool = False, ctx: Context | None = None,
) -> dict[str, Any]:
    """Write the capture time from the file name into JPEG files that have none (changes the files!).

    Lightroom's SDK cannot set the capture time, so this writes EXIF DateTimeOriginal/CreateDate into the file
    with exiftool, after copying the original to the backup folder (restored if exiftool fails). Uses the kind
    date_in_name of the last catalog check (names like 2019-12-08_19-35-23_000.jpeg); names without a time,
    files that already have a capture time and anything that is not a JPEG are skipped. limit defaults to 1
    (try one photo first), at most 50. year / only_wrong_folder narrow the list. Afterwards the written photos
    are in the collection "Prüfen › Sonstiges › Datum in Datei geschrieben": select it in Lightroom and use
    Metadata > Read Metadata from Files. Dry run unless confirm=true; needs the operator opt-in for edits
    and for file edits (--allow-file-edits).
    """
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    if not permissions.file_edits_enabled():
        return {"error": permissions.file_edits_disabled_message(), "code": "disabled"}
    exiftool = file_dates.exiftool_path()
    if not exiftool:
        return {"error": "exiftool is not installed on the Mac. Install it with: brew install exiftool",
                "code": "missing_tool"}
    saved = catalog_audit.load_result()
    if not saved:
        return {"error": "No finished catalog check yet. Start one with catalog_audit_start."}
    timed, date_only = _name_date_items(saved, year, only_wrong_folder)
    already = file_dates.done_ids()  # written earlier; the catalog check lists them until Lightroom read the files
    timed = [f for f in timed if f["id"] not in already]
    batch = timed[: max(1, min(limit, MAX_FILE_BATCH))]
    if not batch:
        return {"nothing_to_do": True, "candidates": len(timed), "date_only_skipped": date_only,
                "already_written": len(already)}
    paths = await _run(_adapter.photo_paths, [f["id"] for f in batch])
    if _is_error(paths):
        return paths

    def inspect() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        ready, skipped = [], []
        for f in batch:
            path = (paths.get("paths") or {}).get(f["id"])
            problem = file_dates.check_file(path, f["file"])
            if not problem:
                try:
                    if file_dates.existing_time(exiftool, str(path)):
                        problem = "hat schon eine Aufnahmezeit in der Datei"
                except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    problem = f"exiftool: {exc}"
            if problem:
                skipped.append({"file": f["file"], "reason": problem, "id": f["id"]})
            else:
                ready.append({"id": f["id"], "file": f["file"], "path": str(path), "set_to": f["name_date"]})
        return ready, skipped

    ready, skipped = await anyio.to_thread.run_sync(inspect)
    plan = {"candidates": len(timed), "date_only_skipped": date_only, "already_written": len(already),
            "this_call": len(batch),
            "will_write": [{"file": r["file"], "folder": short_path(r["path"]), "set_to": r["set_to"]} for r in ready[:10]],
            "skipped": skipped[:10], "backup_folder": str(file_dates.backup_root())}
    if not ready:
        return {"nothing_to_do": True, **plan}
    if not confirm:
        return {"dry_run": True, **plan, "hint": "Nothing was changed. Call again with confirm=true."}
    approved, how = await gate.approve(
        ctx, f"Aufnahmezeit in {len(ready)} JPEG-Dateien schreiben (Sicherung vorher): "
             + ", ".join(r["file"] for r in ready[:5]))
    if not approved:
        return gate.refusal(how)

    def write() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        done, failed = [], []
        for r in ready:
            try:
                done.append({**r, **file_dates.write_time(exiftool, r["path"], r["set_to"])})
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                failed.append({"file": r["file"], "error": str(exc)})
        return done, failed

    # photos whose file already holds a capture time need nothing from this tool: do not offer them again
    file_dates.remember_done([k["id"] for k in skipped if k["reason"].startswith("hat schon")])
    done, failed = await anyio.to_thread.run_sync(write)
    out: dict[str, Any] = {"applied": bool(done), "written": len(done), "failed": failed,
                           "backup_folder": str(file_dates.backup_root()), "skipped": skipped}
    if done:
        file_dates.remember_done([d["id"] for d in done])
        group = [{"set": "Sonstiges", "name": "Datum in Datei geschrieben", "photo_ids": [d["id"] for d in done]}]
        made = await _run(_adapter.create_review_collections, catalog_audit.ROOT_SET, group)
        out["collection"] = ("Prüfen › Sonstiges › Datum in Datei geschrieben" if not _is_error(made)
                             else f"nicht angelegt: {made['error']}")
        out["next"] = ("In Lightroom die Sammlung markieren (Cmd+A), dann Metadaten > Metadaten aus Datei lesen; "
                       "danach get_photo_metadata prüfen und die Katalog-Prüfung wiederholen.")
        out["change_id"] = audit.record("write_name_dates_to_files", [d["id"] for d in done],
                                        {"files": [{"file": d["file"], "set_to": d["set_to"], "backup": d["backup"]}
                                                   for d in done]}, approved_by=how, result={"written": len(done)})
    return out


def short_path(path: str) -> str:
    parts = Path(path).parts[:-1]
    return "/".join(parts[-3:])


@mcp.tool()
async def rotate_photos(
    photo_ids: list[str], degrees: int = 180, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Rotate up to 100 photos clockwise by 90, 180 or 270 degrees (for photos shown sideways or upside down).

    The result lists how many were rotated and why any failed. Turning back is another rotation (180 undoes 180,
    270 undoes 90). Dry run unless confirm=true; needs operator opt-in.
    """
    if degrees not in (90, 180, 270):
        return {"error": "degrees must be 90, 180 or 270 (clockwise)"}
    return await _edit(
        photo_ids, {"rotate_degrees_clockwise": degrees}, lambda: _adapter.rotate_photos(photo_ids, degrees),
        confirm, ctx=ctx, tool="rotate_photos", fields=(),
        summary=f"Drehen um {degrees}° im Uhrzeigersinn",
    )


# ---------------------------------------------------------------- catalog audit
AUDIT_KINDS = ", ".join(catalog_audit.KINDS)


@mcp.tool()
async def catalog_audit_start(year: int | None = None, check_files: bool = False) -> dict[str, Any]:
    """Start a read-only check of the whole catalog: photos in the wrong date folder, and duplicates.

    Folders are JJJJ-JJJJ/JJJJ/JJJJ-MM/JJJJ-MM-TT (decade, year, month, day; e.g.
    2020-2029/2026/2026-10/2026-10-01); the capture time decides where a photo belongs. Runs in the
    background (about 1000 photos per page; 250k+ photos take several minutes), follow it with
    catalog_audit_status, then read catalog_audit_results. year limits the run to one year (by
    capture date or year folder): much faster, because details are read only for that year; duplicates
    are then only searched within the year. check_files also looks whether each file
    still exists on disk (slow over a NAS). Duplicate candidates (same second, camera, pixel size,
    file type and byte size) are confirmed by a fingerprint of the first and last MiB of each file.
    Changes nothing; only one run at a time.
    """
    if year is not None and not 1900 <= year <= 2200:
        return {"error": "year must be four digits"}
    adapter = _adapter
    started, job = catalog_audit.start(
        lambda offset, limit, refresh: adapter.audit_page(offset, limit, refresh, year), year, check_files,
        lambda y, start: adapter.audit_scan(y, start))
    return {"started": started, **job.status(),
            **({} if started else {"hint": "A run is already in progress; see catalog_audit_status."})}


@mcp.tool()
async def catalog_audit_cancel() -> dict[str, Any]:
    """Stop the running catalog check at its next step (seconds). Changes nothing in the catalog.

    A Lightroom call that is already running finishes first. Afterwards a new run can be started;
    catalog_audit_status then shows status "cancelled".
    """
    job = catalog_audit.cancel()
    if job is None:
        return {"cancelled": False, "hint": "No catalog check is running."}
    return {"cancelled": True, "note": "Stops at the next step; check catalog_audit_status.", **job.status()}


@mcp.tool()
async def catalog_audit_status() -> dict[str, Any]:
    """Progress of the running catalog check, or the summary of the last finished one.

    A run with year first scans the whole catalog by capture year (phase "scanning catalog by capture
    year", scanned / total_photos / matches, percent), then reads the details of the matching photos.
    """
    job = catalog_audit.current()
    out: dict[str, Any] = job.status() if job else {"status": "none"}
    if out["status"] in ("none", "done", "failed"):
        saved = catalog_audit.load_result()
        if saved:
            out["last_result"] = {k: saved.get(k) for k in ("finished_at", "seconds", "year", "summary",
                                                             "unsupported_keys")}
    return out


@mcp.tool()
async def catalog_audit_results(
    kind: str = "wrong_folder", year: int | None = None, level: str | None = None,
    limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    """Findings of the last catalog check, page by page.

    kind: wrong_folder (entries have found = folder it is in, expected = day folder it belongs in,
    level year|month|day, day_diff for neighbouring days, often a time-zone or midnight effect),
    (level year|month|day = a folder of another date, decade = right year in the wrong or a missing decade
    folder, no_day = right month but not in a day folder), no_date (photos), video_no_date (videos have
    no capture date in Lightroom), date_in_name (no capture date, but the file name carries one:
    name_date, folder_fits), implausible_date, unrecognised_folder, missing_file, near_duplicates (the
    same image with other metadata: file sizes within 1 %, remove = the copies), duplicates (verified groups:
    keep = the copy with the most work in it, remove = the others), possible_duplicates (same
    shot and similar file name but different bytes; look at them with get_contact_sheet first),
    non_photo (screenshots, messenger pictures, downloads; reasons lists the signs), jpeg_with_raw
    (the camera's JPEG next to its RAW; check=true when the JPEG has stars/keywords/edits and the
    RAW has none), reimported_export (a JPEG export of a RAW that came back into the catalog).
    year filters by capture year (wrong_folder: the target year); level filters wrong_folder.
    """
    if kind not in catalog_audit.KINDS:
        return {"error": f"kind must be one of: {AUDIT_KINDS}"}
    saved = catalog_audit.load_result()
    if not saved:
        return {"error": "No finished catalog check yet. Start one with catalog_audit_start."}
    items = saved["findings"].get(kind, [])
    if year is not None:
        key = "expected" if kind == "wrong_folder" else "name_date" if kind == "date_in_name" else "captured"
        items = [i for i in items if str(i.get(key) or "")[:4] == str(year)]
    if level is not None:
        items = [i for i in items if i.get("level") == level]
    if kind == "wrong_folder":
        items = sorted(items, key=lambda i: (i["expected"], i["id"]))
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    page = items[offset : offset + limit]
    out = {"kind": kind, "checked_at": saved.get("finished_at"), "total": len(items), "items": page}
    if offset + len(page) < len(items):
        out["next_offset"] = offset + len(page)
    return out


@mcp.tool()
async def catalog_audit_collections(
    kind: str, year: int | None = None, level: str | None = None, confirm: bool = False,
    ctx: Context | None = None
) -> dict[str, Any]:
    """Put findings of the last catalog check into review collections below the set "Prüfen".

    wrong_folder: "Prüfen › Ordner falsch › → 2021-07-14" (one per target day; per month when there
    are more than 300 days), so you can drag each collection onto its folder in Lightroom's Folders
    panel (Lightroom moves the files and keeps the catalog in order; never move them in Finder).
    duplicates: "Prüfen › Duplikate › entfernen" and "behalten (Gegenstück)"; possible_duplicates
    and the other kinds get one collection each; jpeg_with_raw: "JPEG neben RAW – entfernen" and
    "– erst prüfen" (JPEGs with work in them). Moves and deletes nothing: the Lightroom SDK
    cannot do either. Additive; running again adds to the same collections. year: only that
    capture year. level (wrong_folder only): year, month, day, decade or no_day, so each kind of
    mistake can be handled on its own. near_duplicates: "nahezu gleich – entfernen" / "– behalten".
    Dry run unless confirm=true; needs operator opt-in.
    """
    if kind not in catalog_audit.KINDS:
        return {"error": f"kind must be one of: {AUDIT_KINDS}"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    saved = catalog_audit.load_result()
    if not saved:
        return {"error": "No finished catalog check yet. Start one with catalog_audit_start."}
    if year is not None and kind != "wrong_folder":
        saved = {**saved, "findings": {**saved["findings"], kind: [
            i for i in saved["findings"].get(kind, [])
            if str(i.get("name_date" if kind == "date_in_name" else "captured") or "")[:4] == str(year)]}}
    groups, mode = catalog_audit.review_groups(saved, kind, year, level)
    groups = [g for g in groups if g["photo_ids"]]
    photos = sum(len(g["photo_ids"]) for g in groups)
    plan = {"kind": kind, "checked_at": saved.get("finished_at"), "root": catalog_audit.ROOT_SET,
            "collections": len(groups), "photos": photos, "layout": mode,
            "first_collections": [f"{g['set']} › {g['name']} ({len(g['photo_ids'])})" for g in groups[:15]]}
    if not groups:
        return {**plan, "nothing_to_do": True}
    if not confirm:
        return {"dry_run": True, **plan, "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(
        ctx, f"{len(groups)} Prüf-Sammlungen mit {photos} Fotos unter '{catalog_audit.ROOT_SET}' anlegen")
    if not approved:
        return gate.refusal(how)
    added = missing = 0
    for call in catalog_audit.chunk_groups(groups):
        res = await _run(_adapter.create_review_collections, catalog_audit.ROOT_SET, call)
        if _is_error(res):
            return {**res, "partly_done": {"photos_added": added}} if added else res
        added += sum(c["added"] for c in res["collections"])
        missing += int(res.get("missing") or 0)
    result = {"collections": len(groups), "photos_added": added, "photos_no_longer_in_catalog": missing}
    change_id = audit.record("catalog_audit_collections", [], {"kind": kind, "year": year, "layout": mode},
                             approved_by=how, result=result)
    return {"applied": True, "change_id": change_id, **plan, **result,
            "note": "Not undoable here; delete the set 'Prüfen' in Lightroom when you are done."}


@mcp.tool()
async def list_changes(limit: int = 10) -> dict[str, Any]:
    """Recent confirmed changes from the local audit log (newest first), with undo status."""
    return {"changes": audit.recent(max(1, min(limit, 50))), "log_file": str(audit.log_path())}


def _restore_plan(entry: dict[str, Any], current: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Per photo: what to restore, or why it is skipped (changed since)."""
    tool, change, before = entry["tool"], entry["change"], entry["before"]
    plan = []
    for pid in entry["photo_ids"]:
        cur, prev = current[pid], before.get(pid, {})
        item: dict[str, Any] = {"id": pid}
        if tool == "set_rating":
            ok, item["restore"] = cur.get("rating") == change["rating"], {"rating": prev.get("rating", 0)}
        elif tool == "set_flag":
            ok, item["restore"] = cur.get("flag") == change["flag"], {"flag": prev.get("flag") or "none"}
        elif tool == "set_color_label":
            ok = (cur.get("color_label") or "none") == change["label"]
            item["restore"] = {"color_label": prev.get("color_label") or "none"}
        elif tool == "set_title_caption":
            ok = all((cur.get(k) or "") == v for k, v in change.items())
            item["restore"] = {k: prev.get(k) or "" for k in change}
        elif tool == "add_keywords":
            had = {k.lower() for k in prev.get("keywords") or []}
            added = [k for k in change["add_keywords"] if k.lower() not in had]
            have = {k.lower() for k in cur.get("keywords") or []}
            ok = bool(added) and all(k.lower() in have for k in added)
            item["restore"] = {"remove_keywords": added}
            if not added:
                item["skip"] = "keywords were already present, nothing to remove"
        else:
            ok = False
        if not ok and "skip" not in item:
            item["skip"] = "changed since (or not restorable); left as it is"
        plan.append(item)
    return plan


@mcp.tool()
async def undo_last_change(confirm: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    """Undo the most recent undoable change (rating, flag, color label, title/caption, keywords).

    Photos changed again since are left alone. Presets/develop settings are not covered here:
    undo those in Lightroom's History panel. Dry run unless confirm=true; needs opt-in.
    """
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    entry = audit.last_undoable()
    if entry is None:
        return {"error": "Nothing to undo: no undoable change in the audit log."}
    current: dict[str, dict[str, Any]] = {}
    for pid in entry["photo_ids"]:
        meta = await _run(_adapter.get_photo, pid)
        if _is_error(meta):
            return {"error": f"Cannot undo, photo {pid} is not available: {meta['error']}"}
        current[pid] = meta.metadata()
    plan = _restore_plan(entry, current)
    doable = [p for p in plan if "skip" not in p]
    summary = {"change_id": entry["id"], "tool": entry["tool"], "time": entry.get("ts"), "change": entry["change"]}
    if not doable:
        return {**summary, "error": "No photo can be restored (all changed since).", "photos": plan}
    if not confirm:
        return {"dry_run": True, **summary, "photos": plan, "hint": "Call again with confirm=true to undo."}
    approved, how = await gate.approve(
        ctx, f"Letzte Änderung rückgängig machen: {entry['tool']} für {len(doable)} Foto(s)"
    )
    if not approved:
        return gate.refusal(how)

    groups: dict[str, list[str]] = {}
    for item in doable:
        groups.setdefault(json.dumps(item["restore"], sort_keys=True), []).append(item["id"])
    for key, pids in groups.items():
        restore = json.loads(key)
        tool = entry["tool"]
        if tool == "set_rating":
            result = await _run(_adapter.set_rating, pids, restore["rating"])
        elif tool == "set_flag":
            result = await _run(_adapter.set_flag, pids, restore["flag"])
        elif tool == "set_color_label":
            result = await _run(_adapter.set_color_label, pids, restore["color_label"])
        elif tool == "set_title_caption":
            result = await _run(_adapter.set_title_caption, pids, restore.get("title"), restore.get("caption"))
        else:
            result = await _run(_adapter.remove_keywords, pids, restore["remove_keywords"])
        if _is_error(result):
            return {**result, "note": "Undo stopped part-way; check with list_changes and Lightroom."}
    undo_id = audit.record(
        "undo_last_change", [p["id"] for p in doable], {"undoes": entry["id"]},
        approved_by=how, undoes=entry["id"],
    )
    return {"applied": True, "change_id": undo_id, **summary, "restored": len(doable),
            "skipped": [p for p in plan if "skip" in p]}


EXPORT_PRESETS: dict[str, dict[str, Any]] = {
    "instagram": {"long_edge": 1350, "quality": 90, "sharpen": "screen"},  # feed 4:5 after crop_to_aspect
    "story": {"long_edge": 1920, "quality": 90, "sharpen": "screen"},  # 9:16
    "web": {"long_edge": 2400, "quality": 85, "sharpen": "screen"},
    "full": {},
}


def _export_options(
    preset: str | None, long_edge: int | None, quality: int | None, sharpen: str | None
) -> dict[str, Any]:
    """Export options from a preset plus explicit values (explicit wins); an error dict for bad input."""
    if preset is not None and preset not in EXPORT_PRESETS:
        return {"error": f"preset must be one of: {', '.join(EXPORT_PRESETS)}"}
    options = dict(EXPORT_PRESETS.get(preset or "", {}))
    if long_edge is not None:
        if not 200 <= long_edge <= 10000:
            return {"error": "long_edge must be between 200 and 10000 pixels"}
        options["long_edge"] = long_edge
    if quality is not None:
        if not 1 <= quality <= 100:
            return {"error": "quality must be between 1 and 100"}
        options["quality"] = quality
    if sharpen is not None:
        if sharpen not in ("screen", "off"):
            return {"error": "sharpen must be screen or off"}
        options["sharpen"] = sharpen
    return {"options": options}


@mcp.tool()
async def export_photos(
    photo_ids: list[str], preset: str | None = None, long_edge: int | None = None,
    quality: int | None = None, sharpen: str | None = None,
    confirm: bool = False, ctx: Context | None = None,
) -> dict[str, Any]:
    """Export photos as JPEG into the operator-configured folder. Dry run unless confirm=true.

    The destination cannot be chosen by the client; it comes from MHPHOTOS_EXPORT_DIR. Optional format:
    preset "instagram" (long edge 1350 px), "story" (1920), "web" (2400) or "full" (no resize), or
    long_edge (200-10000 px), quality (1-100), sharpen (screen|off); explicit values win over the preset.
    Photos are never enlarged. Crop first (crop_to_aspect) if the picture should be 4:5 or 9:16.
    """
    if bad := _check_ids(photo_ids):
        return bad
    opts = _export_options(preset, long_edge, quality, sharpen)
    if "error" in opts:
        return opts
    options = opts["options"]
    dest = permissions.export_dir()
    if dest is None:
        return {"error": permissions.export_disabled_message(), "code": "disabled"}
    names = []
    for pid in photo_ids:
        meta = await _run(_adapter.get_photo, pid)
        if _is_error(meta):
            return meta
        names.append({"id": pid, "filename": meta.filename})
    if not confirm:
        return {
            "dry_run": True,
            "destination": dest,
            "files": [n["filename"] for n in names],
            "options": options,
            "hint": "Nothing was exported. Call again with confirm=true to export.",
        }
    approved, how = await gate.approve(ctx, f"{len(names)} Foto(s) als JPEG nach {dest} exportieren: {_names(names)}")
    if not approved:
        return gate.refusal(how)
    Path(dest).mkdir(parents=True, exist_ok=True)
    result = await _run(_adapter.export_photos, photo_ids, dest, options or None)
    if _is_error(result):
        return result
    change_id = audit.record("export_photos", photo_ids, {"destination": dest, **options}, approved_by=how)
    return {"applied": True, "change_id": change_id, **result}


@mcp.tool()
async def send_to_mhphotos_inbox(
    photo_ids: list[str],
    confirm: bool = False,
    mark_in_lightroom: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Export photos and send them to the MHPhotos Foto-Inbox for review (not published).

    Items arrive with status "wartet" and are approved inside the MHPhotos app. Dry run
    unless confirm=true. Needs operator config: MHPHOTOS_INBOX_URL, the admin token and
    MHPHOTOS_EXPORT_DIR. mark_in_lightroom=true adds the keyword "MHPhotos-Inbox" to the sent
    photos (needs MHPHOTOS_ALLOW_WRITES=1) so backlog_report can tell what is already sent.
    """
    if bad := _check_ids(photo_ids):
        return bad
    cfg = publishing.inbox_config()
    dest = permissions.export_dir()
    if cfg is None or dest is None:
        return {"error": publishing.disabled_message(), "code": "disabled"}
    photos = []
    for pid in photo_ids:
        meta = await _run(_adapter.get_photo, pid)
        if _is_error(meta):
            return meta
        photos.append(meta)
    if mark_in_lightroom and not permissions.writes_enabled():
        return {"error": "mark_in_lightroom needs " + permissions.writes_disabled_message(), "code": "disabled"}
    if not confirm:
        return {
            "dry_run": True,
            "target": cfg.host,
            "files": [p.filename for p in photos],
            "mark_in_lightroom": mark_in_lightroom,
            "hint": "Nothing was exported or sent. Call again with confirm=true.",
        }
    approved, how = await gate.approve(
        ctx,
        f"{len(photos)} Foto(s) exportieren und an die MHPhotos-Inbox ({cfg.host}) senden "
        f"(noch nicht öffentlich): {_names([{'filename': p.filename} for p in photos])}",
    )
    if not approved:
        return gate.refusal(how)
    batch_dir = Path(dest) / f"inbox-{uuid.uuid4().hex[:8]}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    exported = await _run(_adapter.export_photos, photo_ids, str(batch_dir))
    if _is_error(exported):
        return exported
    results = []
    for photo in photos:
        stem = photo.filename.rsplit(".", 1)[0]
        path = batch_dir / f"{stem}.jpg"
        if not path.is_file():
            results.append({"photo_id": photo.id, "status": "error", "error": "exported file not found"})
            continue
        outcome = await anyio.to_thread.run_sync(
            publishing.upload, cfg, path, photo.metadata()
        )
        results.append({"photo_id": photo.id, "filename": path.name, **outcome})
    sent = [r["photo_id"] for r in results if r.get("status") in ("uploaded", "duplicate")]
    marked = None
    if mark_in_lightroom and sent:
        mark = await _run(_adapter.add_keywords, sent, [INBOX_KEYWORD])
        marked = {"error": mark["error"]} if _is_error(mark) else {"keyword": INBOX_KEYWORD, "photos": len(sent)}
    change_id = audit.record(
        "send_to_mhphotos_inbox", photo_ids, {"target": cfg.host, "sent": len(sent)},
        approved_by=how, result={"inbox_ids": {r["photo_id"]: r.get("inbox_id") for r in results}},
    )
    out = {"applied": True, "change_id": change_id, "target": cfg.host, "export_folder": str(batch_dir), "results": results}
    if marked is not None:
        out["marked"] = marked
    return out


# ---------------------------------------------------------------- editing advice
MAX_PREVIEWS = 12
MAX_SETTINGS_BATCH = 20
INBOX_KEYWORD = "MHPhotos-Inbox"


@mcp.tool(structured_output=False)  # mixed text + image blocks cannot be structured output
async def get_photo_previews(photo_ids: list[str], size: int = 768) -> list[Any]:
    """Show photos so you can judge and compare them (up to 12 per call, size 256-1280 px).

    Each image is preceded by a caption with id, filename, rating, place and exposure.
    For large albums narrow down first with list_photos filters (min_rating, location, ...).
    """
    if not photo_ids or len(photo_ids) > MAX_PREVIEWS or len(set(photo_ids)) != len(photo_ids):
        return [{"error": f"Provide 1-{MAX_PREVIEWS} distinct photo ids"}]
    size = max(256, min(size, 1280))
    out: list[Any] = []
    for pid in photo_ids:
        meta = await _run(_adapter.get_photo, pid)
        if _is_error(meta):
            out.append(meta)
            continue
        data = await _run(_adapter.get_preview, pid, size)
        if _is_error(data):
            out.append({"id": pid, **data})
            continue
        m = meta.metadata()
        caption = {
            k: m[k]
            for k in ("id", "filename", "rating", "flag", "capture_time", "location", "iso",
                      "aperture", "shutter_speed", "focal_length_mm", "lens")
            if k in m and m[k] not in ("", None)
        }
        out.append(caption)
        out.append(Image(data=data, format="jpeg"))
    return out


@mcp.tool(structured_output=False)  # text + image blocks
async def get_contact_sheet(photo_ids: list[str], columns: int = 5, cell: int = 360) -> list[Any]:
    """One numbered overview image of up to 30 photos (about 5x cheaper than separate previews).

    Use it to pick candidates from a big album, then look closely at the few with
    get_photo_previews. The legend maps each number on the image to the photo id.
    """
    if not photo_ids or len(photo_ids) > imaging.MAX_SHEET_PHOTOS or len(set(photo_ids)) != len(photo_ids):
        return [{"error": f"Provide 1-{imaging.MAX_SHEET_PHOTOS} distinct photo ids"}]
    cell = max(160, min(cell, 480))
    previews: list[bytes | None] = []
    legend: list[dict[str, Any]] = []
    for number, pid in enumerate(photo_ids, 1):
        meta = await _run(_adapter.get_photo, pid)
        data = None if _is_error(meta) else await _run(_adapter.get_preview, pid, max(256, cell))
        if _is_error(meta) or _is_error(data):
            previews.append(None)
            legend.append({"n": number, "id": pid, "error": "not available"})
            continue
        previews.append(data)
        m = meta.metadata()
        legend.append({"n": number, "id": pid, "filename": m["filename"], "rating": m["rating"],
                       **({"location": m["location"]} if m.get("location") else {})})
    sheet = await anyio.to_thread.run_sync(imaging.contact_sheet, previews, columns, cell)
    return [{"legend": legend}, Image(data=sheet, format="jpeg")]


@mcp.tool()
async def get_develop_settings(photo_id: str) -> dict[str, Any]:
    """Current develop settings of a photo (exposure, tone, presence, WB, HSL, ...)."""
    result = await _run(_adapter.get_develop_settings, photo_id)
    if _is_error(result):
        return result
    keep = develop.readable_keys()
    return {"photo_id": photo_id, "settings": {k: v for k, v in result.items() if k in keep}}


@mcp.tool()
async def list_presets(name_contains: str | None = None) -> dict[str, Any]:
    """List the Lightroom develop presets available to apply (folder and name)."""
    result = await _run(_adapter.list_presets)
    if _is_error(result):
        return result
    needle = (name_contains or "").strip().lower()
    return {"presets": [p for p in result if needle in p["name"].lower()]}


@mcp.tool()
async def apply_preset(
    photo_ids: list[str], preset: str, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Apply an existing develop preset ("Name" or "Folder/Name") to photos.

    Dry run unless confirm=true; needs operator opt-in. Lightroom records it as a history
    step ("MHPhotos"), so it can be undone in the Develop module.
    """
    if not preset or len(preset) > 160:
        return {"error": "preset name required"}
    available = await _run(_adapter.list_presets)
    if _is_error(available):
        return available
    hits = [p for p in available if preset in (p["name"], f"{p['folder']}/{p['name']}")]
    if not hits:
        return {"error": f"Preset not found: {preset}. Use list_presets."}
    if len(hits) > 1:
        return {"error": f"Preset name is ambiguous, use Folder/Name: {preset}"}
    return await _edit(
        photo_ids,
        {"preset": f"{hits[0]['folder']}/{hits[0]['name']}"},
        lambda: _adapter.apply_preset(photo_ids, preset),
        confirm,
        ctx=ctx,
        tool="apply_preset",
        summary=f"Preset '{hits[0]['name']}' anwenden (in Lightroom über den Verlauf rückgängig zu machen)",
    )


async def _apply_settings(
    photo_ids: list[str],
    clean: dict[str, Any],
    confirm: bool,
    ctx: Context | None,
    *,
    tool: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not photo_ids or len(photo_ids) > MAX_SETTINGS_BATCH or len(set(photo_ids)) != len(photo_ids):
        return {"error": f"Provide 1-{MAX_SETTINGS_BATCH} distinct photo ids"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    current = []
    for pid in photo_ids:
        cur = await _run(_adapter.get_develop_settings, pid)
        if _is_error(cur):
            return cur
        current.append({"id": pid, "old": {k: cur.get(k) for k in clean}})
    if not confirm:
        return {
            "dry_run": True,
            "new": clean,
            **(extra or {}),
            "photos": current,
            "hint": "Nothing was changed. Call again with confirm=true to apply.",
        }
    approved, how = await gate.approve(
        ctx,
        f"{len(clean)} Entwicklungs-Werte auf {len(photo_ids)} Foto(s) anwenden "
        "(in Lightroom über den Verlauf rückgängig zu machen)",
    )
    if not approved:
        return gate.refusal(how)
    result = await _run(_adapter.apply_develop_settings, photo_ids, clean)
    if _is_error(result):
        return result
    change_id = audit.record(
        tool, photo_ids, {"settings": clean, **(extra or {})},
        before={c["id"]: c["old"] for c in current}, approved_by=how, result=result,
    )
    return {"applied": True, "change_id": change_id, "new": clean, **result}


@mcp.tool()
async def apply_develop_settings(
    photo_ids: list[str],
    settings: dict[str, Any],
    confirm: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Apply develop settings (e.g. {"Exposure2012": 0.3, "Highlights2012": -40}) to photos.

    Only known parameters within Lightroom's ranges are accepted (error lists them). Dry run
    shows old and new values per photo. Needs operator opt-in and confirm=true; Lightroom
    records a history step that can be undone.
    """
    clean, err = develop.validate(settings)
    if err:
        return {"error": err}
    return await _apply_settings(photo_ids, clean, confirm, ctx, tool="apply_develop_settings")


@mcp.tool()
async def sync_develop_settings(
    from_photo_id: str,
    to_photo_ids: list[str],
    include_white_balance: bool = False,
    keys: list[str] | None = None,
    confirm: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Copy the look of one photo to others (tone, presence, HSL, detail, vignette, grain).

    White balance (Temperature/Tint) is left out unless include_white_balance=true, because it
    differs per photo. keys limits the copy to named parameters. Dry run shows old and new values.
    """
    if from_photo_id in to_photo_ids:
        return {"error": "to_photo_ids must not contain from_photo_id"}
    source = await _run(_adapter.get_develop_settings, from_photo_id)
    if _is_error(source):
        return source
    tunable = set(develop.RANGES) | develop.BOOLEANS
    if keys:
        bad = [k for k in keys if k not in tunable]
        if bad:
            return {"error": f"Not copyable: {', '.join(bad)}"}
        wanted = set(keys)
    else:
        wanted = tunable - (set() if include_white_balance else {"Temperature", "Tint"})
    picked = {k: v for k, v in source.items() if k in wanted}
    clean, err = develop.validate(picked) if picked else ({}, "The source photo has none of the copyable settings")
    if err:
        return {"error": err}
    return await _apply_settings(
        to_photo_ids, clean, confirm, ctx, tool="sync_develop_settings", extra={"from": from_photo_id}
    )


@mcp.tool()
async def create_preset(
    name: str, settings: dict[str, Any], confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Create a new develop preset in Lightroom from settings (same parameters as apply_develop_settings).

    It is usable by apply_preset right away and is also written as an XMP preset file, which
    Lightroom Classic lists in the preset group "MHPhotos" after its next start. Dry run unless
    confirm=true; needs operator opt-in. Creating a preset changes no photo.
    """
    if not develop.valid_preset_name(name):
        return {"error": f"name required (max {develop.MAX_PRESET_NAME} characters, no '/')"}
    clean, err = develop.validate(settings)
    if err:
        return {"error": err}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    existing = await _run(_adapter.list_presets)
    if _is_error(existing):
        return existing
    if any(p["name"] == name.strip() for p in existing):
        return {"error": f"A preset named '{name.strip()}' already exists. Choose another name."}
    if not confirm:
        return {"dry_run": True, "name": name.strip(), "settings": clean,
                "hint": "No preset was created. Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"Preset '{name.strip()}' mit {len(clean)} Werten in Lightroom anlegen")
    if not approved:
        return gate.refusal(how)
    result = await _run(_adapter.create_preset, name.strip(), clean)
    if _is_error(result):
        return result
    # The SDK preset is usable by apply_preset in this Lightroom run but not shown in the Develop
    # panel; the XMP file makes it a normal, visible preset after the next Lightroom start.
    try:
        written = preset_xmp.write(name.strip(), clean)
    except OSError as exc:
        written = {"preset_file": None, "preset_file_note": f"XMP preset not written: {exc.strerror or exc}"}
    change_id = audit.record("create_preset", [], {"name": name.strip(), "settings": clean,
                             "preset_file": written.get("preset_file")}, approved_by=how)
    return {"applied": True, "change_id": change_id, **result, **written}


# ---------------------------------------------------------------- MHPhotos app
MAX_INBOX_BATCH = 20
_OVERRIDE_KEYS = {"title": 120, "description": 2000, "alt": 220, "location": 120}


def _app_cfg() -> publishing.InboxConfig | dict[str, Any]:
    cfg = publishing.inbox_config()
    if cfg is None:
        return {"error": publishing.config_missing_message(), "code": "disabled"}
    return cfg


def _app_error(status: int, data: dict[str, Any]) -> dict[str, Any]:
    if status == 0:
        return {"error": data.get("error", "MHPhotos app not reachable"), "code": "unavailable"}
    if status in (401, 403):
        return {"error": "MHPhotos rejected the admin token", "code": "unauthorized"}
    return {"error": str(data.get("error") or f"MHPhotos answered HTTP {status}"), "code": "app_error"}


def _suggestion(item: dict[str, Any]) -> dict[str, Any] | None:
    sug = item.get("aiSuggestion")
    if not isinstance(sug, dict):
        return None
    ids = [g for g in (sug.get("galleryIds") or []) if g] or (
        [sug["galleryId"]] if sug.get("galleryId") else []
    )
    return {
        "title": sug.get("title", ""),
        "description": sug.get("description", ""),
        "alt": sug.get("alt", ""),
        "tags": sug.get("tags") or [],
        "gallery_ids": ids,
        "gallery_titles": sug.get("galleryTitles") or ([sug["galleryTitle"]] if sug.get("galleryTitle") else []),
        "new_gallery_title": sug.get("newGalleryTitle"),
        "confidence": sug.get("confidence"),
    }


def _item_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "filename": item.get("originalFileName"),
        "status": item.get("status"),
        "rating": item.get("rating"),
        "capture_date": item.get("captureDate"),
        "suggestion": _suggestion(item),
        "homepage_photo_id": item.get("homepagePhotoId"),
    }


@mcp.tool()
async def mhphotos_galleries() -> dict[str, Any]:
    """List the MHPhotos galleries/categories (id, title, area) that photos can be filed under."""
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    status, data = await anyio.to_thread.run_sync(publishing.galleries, cfg)
    if status != 200:
        return _app_error(status, data)
    return {
        "galleries": [
            {"id": g.get("id"), "title": g.get("title"), "area": g.get("area"), "parent": g.get("parent")}
            for g in data.get("galleries", [])
        ]
    }


@mcp.tool()
async def mhphotos_inbox(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List Foto-Inbox items with AI suggestions (title, description, categories).

    status filter: wartet | spaeter | uebernommen (published) | abgelehnt.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    code, data = await anyio.to_thread.run_sync(publishing.inbox_items, cfg)
    if code != 200:
        return _app_error(code, data)
    items = [i for i in data.get("items", []) if status is None or i.get("status") == status]
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    return {"total": len(items), "items": [_item_summary(i) for i in items[:limit]]}


@mcp.tool()
async def mhphotos_analyze_inbox(item_ids: list[str]) -> dict[str, Any]:
    """Ask MHPhotos to generate AI title, description and category suggestions for inbox items.

    Runs in the background on the MHPhotos side: call mhphotos_inbox again after a minute
    to read the suggestions. Changes nothing public.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    if not item_ids or len(item_ids) > MAX_INBOX_BATCH or len(set(item_ids)) != len(item_ids):
        return {"error": f"Provide 1-{MAX_INBOX_BATCH} distinct inbox item ids"}
    results = []
    for item_id in item_ids:
        code, data = await anyio.to_thread.run_sync(publishing.request_analysis, cfg, item_id)
        if code in (200, 202) and data.get("status") != "rejected_input":
            results.append({"id": item_id, "status": "queued" if code == 202 else "done"})
        elif code == 200:
            results.append({"id": item_id, "status": "rejected", "error": data.get("error")})
        else:
            results.append({"id": item_id, "status": "error", **_app_error(code, data)})
    return {"results": results}


def _clean_overrides(overrides: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    clean: dict[str, dict[str, Any]] = {}
    for item_id, ov in (overrides or {}).items():
        out: dict[str, Any] = {}
        for key, max_len in _OVERRIDE_KEYS.items():
            if key in ov:
                value = str(ov[key]).strip()
                if len(value) > max_len:
                    return {"error": f"override '{key}' for {item_id} is longer than {max_len} characters"}
                out[key] = value
        if "categories" in ov:
            cats = ov["categories"]
            if not isinstance(cats, list) or not all(isinstance(c, str) for c in cats):
                return {"error": f"override 'categories' for {item_id} must be a list of gallery ids"}
            out["categories"] = cats
        clean[item_id] = out
    return {"overrides": clean}


@mcp.tool()
async def mhphotos_publish(
    item_ids: list[str],
    overrides: dict[str, dict[str, Any]] | None = None,
    confirm: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Publish approved inbox items on the PUBLIC MHPhotos site (mhphotos.de).

    Uses the AI suggestion (title, description, alt, categories) unless overridden per
    item: overrides = {item_id: {title, description, alt, location, categories}}.
    Dry run unless confirm=true; needs MHPHOTOS_ALLOW_PUBLISH=1 from the operator.
    Items that are already published, rejected, lack a title or a valid category are
    skipped and reported.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    if not publishing.publishing_enabled():
        return {"error": publishing.publish_disabled_message(), "code": "disabled"}
    if not item_ids or len(item_ids) > MAX_INBOX_BATCH or len(set(item_ids)) != len(item_ids):
        return {"error": f"Provide 1-{MAX_INBOX_BATCH} distinct inbox item ids"}
    cleaned = _clean_overrides(overrides)
    if "error" in cleaned:
        return cleaned
    ov_all = cleaned["overrides"]
    unknown_ov = set(ov_all) - set(item_ids)
    if unknown_ov:
        return {"error": f"overrides given for items not in item_ids: {', '.join(sorted(unknown_ov))}"}

    code, inbox = await anyio.to_thread.run_sync(publishing.inbox_items, cfg)
    if code != 200:
        return _app_error(code, inbox)
    code, gal = await anyio.to_thread.run_sync(publishing.galleries, cfg)
    if code != 200:
        return _app_error(code, gal)
    items = {i.get("id"): i for i in inbox.get("items", [])}
    valid_galleries = {g.get("id") for g in gal.get("galleries", [])}

    plan: list[dict[str, Any]] = []
    for item_id in item_ids:
        item = items.get(item_id)
        entry: dict[str, Any] = {"id": item_id}
        plan.append(entry)
        if item is None:
            entry["problem"] = "not found in inbox"
            continue
        entry["filename"] = item.get("originalFileName")
        if item.get("status") == "uebernommen":
            entry["problem"] = "already published"
            continue
        if item.get("status") == "abgelehnt":
            entry["problem"] = "was rejected in MHPhotos"
            continue
        sug = _suggestion(item) or {}
        ov = ov_all.get(item_id, {})
        title = ov.get("title") or sug.get("title") or item.get("title") or ""
        categories = ov.get("categories") or sug.get("gallery_ids") or []
        entry.update(
            title=title,
            description=ov.get("description", sug.get("description") or item.get("description") or ""),
            alt=ov.get("alt", sug.get("alt") or title),
            location=ov.get("location", item.get("location") or ""),
            categories=categories,
            source="override" if ov else ("ai_suggestion" if sug else "none"),
        )
        if not title:
            entry["problem"] = "no title: run mhphotos_analyze_inbox first or pass an override"
        elif not categories:
            entry["problem"] = "no category: pass overrides[...]['categories'] (see mhphotos_galleries)"
        elif not set(categories) <= valid_galleries:
            entry["problem"] = "unknown category: " + ", ".join(sorted(set(categories) - valid_galleries))

    ready = [e for e in plan if "problem" not in e]
    if not confirm:
        return {
            "dry_run": True,
            "will_be_public": True,
            "ready": len(ready),
            "items": plan,
            "hint": "Nothing was published. Review title, description and categories, then call again with confirm=true.",
        }
    if not ready:
        return {"applied": False, "published": 0, "items": plan, "hint": "Nothing is ready to publish."}
    titles = "; ".join(f"{e['title']} -> {', '.join(e['categories'])}" for e in ready[:6])
    approved, how = await gate.approve(
        ctx, f"{len(ready)} Foto(s) ÖFFENTLICH auf mhphotos.de veröffentlichen: {titles}"
    )
    if not approved:
        return gate.refusal(how)
    for entry in ready:
        fields = {k: entry[k] for k in ("title", "description", "alt", "location", "categories")}
        code, data = await anyio.to_thread.run_sync(publishing.approve, cfg, entry["id"], fields)
        if code == 200 and data.get("ok"):
            entry["status"] = "published"
            entry["homepage_photo_id"] = (data.get("photo") or {}).get("id")
        else:
            entry["status"] = "error"
            entry["problem"] = _app_error(code, data)["error"]
    published = sum(1 for e in plan if e.get("status") == "published")
    change_id = audit.record(
        "mhphotos_publish", [], {"items": [{"id": e["id"], "title": e.get("title"), "status": e.get("status")} for e in ready]},
        approved_by=how,
    )
    return {"applied": True, "change_id": change_id, "published": published, "items": plan}


@mcp.tool()
async def mhphotos_instagram_posts(
    year: int | None = None,
    only_not_on_homepage: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List your Instagram posts known to MHPhotos (library synced inside the MHPhotos app).

    By default only posts that are not yet linked to a photo on the MHPhotos site.
    The library is as fresh as the last sync in the MHPhotos app (see newest_sync).
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    code, data = await anyio.to_thread.run_sync(publishing.instagram_library, cfg)
    if code != 200:
        return _app_error(code, data)
    items = data.get("items", [])
    newest_sync = max((i.get("syncedAt") or "" for i in items), default="") or None
    if year is not None:
        items = [i for i in items if str(i.get("timestamp") or "").startswith(str(year))]
    if only_not_on_homepage:
        items = [i for i in items if not i.get("homepagePhotoId")]
    items = sorted(items, key=lambda i: i.get("timestamp") or "", reverse=True)
    # Posts whose Lightroom original already waits in the MHPhotos inbox (exported with
    # instagram_post_id): matching them again is not needed. Best-effort.
    pending: dict[str, str] = {}
    icode, idata = await anyio.to_thread.run_sync(publishing.inbox_items, cfg)
    if icode == 200:
        pending = {str(x["instagramId"]): x.get("status", "") for x in idata.get("items", []) if x.get("instagramId")}
    limit, offset = _clamp(limit, offset)
    return {
        "total": len(items),
        "newest_sync": newest_sync,
        "posts": [
            {
                "id": i.get("id"),
                "permalink": i.get("permalink"),
                "timestamp": i.get("timestamp"),
                "media_type": i.get("mediaType"),
                "caption": (i.get("caption") or "")[:300],
                "thumbnail_url": i.get("thumbnailUrl") or i.get("mediaUrl"),
                "homepage_photo_id": i.get("homepagePhotoId"),
                "in_inbox": pending.get(str(i.get("id"))),
            }
            for i in items[offset : offset + limit]
        ],
    }


@mcp.tool(structured_output=False)  # text + image blocks
async def mhphotos_instagram_images(post_ids: list[str]) -> list[Any]:
    """Show Instagram posts (up to 6 per call) so you can compare them with Lightroom previews.

    Use mhphotos_instagram_posts for the ids. To find the Lightroom original of a post:
    shortlist with list_photos (captured_from/captured_to in the weeks BEFORE the post date,
    min_rating, location), look at them with get_photo_previews and compare. Instagram
    crops (often 4:5) and recompresses, so judge subject and composition, not pixels.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return [cfg]
    if not post_ids or len(post_ids) > 6 or len(set(post_ids)) != len(post_ids):
        return [{"error": "Provide 1-6 distinct Instagram post ids"}]
    code, data = await anyio.to_thread.run_sync(publishing.instagram_library, cfg)
    if code != 200:
        return [_app_error(code, data)]
    by_id = {i.get("id"): i for i in data.get("items", [])}
    out: list[Any] = []
    for pid in post_ids:
        item = by_id.get(pid)
        if item is None:
            out.append({"id": pid, "error": "post not found in the MHPhotos Instagram library"})
            continue
        url = item.get("mediaUrl") if item.get("mediaType") == "IMAGE" else None
        url = url or item.get("thumbnailUrl") or item.get("mediaUrl") or ""
        image, reason = await anyio.to_thread.run_sync(publishing.fetch_image, url)
        if image is None:
            out.append({"id": pid, "error": reason})
            continue
        out.append(
            {
                "id": pid,
                "timestamp": item.get("timestamp"),
                "permalink": item.get("permalink"),
                "media_type": item.get("mediaType"),
                "caption": (item.get("caption") or "")[:300],
            }
        )
        out.append(Image(data=image, format="jpeg"))
    return out


# ---------------------------------------------------------------- inbox images (no Lightroom needed)
MAX_SHEET = 30


async def _inbox_items_by_id(cfg: publishing.InboxConfig) -> dict[str, dict[str, Any]] | dict[str, Any]:
    code, data = await anyio.to_thread.run_sync(publishing.inbox_items, cfg)
    if code != 200:
        return {"_error": _app_error(code, data)}
    return {i.get("id"): i for i in data.get("items", [])}


def _inbox_caption(item: dict[str, Any]) -> dict[str, Any]:
    s = _item_summary(item)
    for key in ("source", "camera", "location", "lens"):
        if item.get(key):
            s[key] = item[key]
    return {k: v for k, v in s.items() if v not in (None, "", [])}


async def _original(cfg: publishing.InboxConfig, item_id: str) -> bytes | dict[str, Any]:
    code, data = await anyio.to_thread.run_sync(publishing.inbox_image, cfg, item_id)
    if data is None:
        return {"id": item_id, **_app_error(code, {"error": f"image of {item_id} not available"})}
    return data


@mcp.tool(structured_output=False)  # text + image blocks
async def mhphotos_inbox_images(item_ids: list[str], size: int = 768) -> list[Any]:
    """Look at up to 12 Foto-Inbox items as images (the originals shrunk to `size`, 256-1280 px).

    Works for photos that arrived from anywhere (Lightroom Classic plugin, the Mac sync service,
    Lightroom Mobile exports) without Lightroom running. Each image comes with a caption.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return [cfg]
    if not item_ids or len(item_ids) > MAX_PREVIEWS or len(set(item_ids)) != len(item_ids):
        return [{"error": f"Provide 1-{MAX_PREVIEWS} distinct inbox item ids"}]
    items = await _inbox_items_by_id(cfg)
    if "_error" in items:
        return [items["_error"]]
    size = max(256, min(size, 1280))
    out: list[Any] = []
    for iid in item_ids:
        if iid not in items:
            out.append({"id": iid, "error": "not found in inbox"})
            continue
        raw = await _original(cfg, iid)
        if isinstance(raw, dict):
            out.append(raw)
            continue
        try:
            jpeg = await anyio.to_thread.run_sync(imaging.downscale_jpeg, raw, size)
        except (OSError, ValueError):
            out.append({"id": iid, "error": "image could not be decoded"})
            continue
        out.append(_inbox_caption(items[iid]))
        out.append(Image(data=jpeg, format="jpeg"))
    return out


@mcp.tool(structured_output=False)  # text + image blocks
async def mhphotos_inbox_contact_sheet(
    item_ids: list[str], columns: int = 5, cell: int = 360
) -> list[Any]:
    """One numbered overview image of up to 30 inbox items, to pick the best of a large batch cheaply.

    Look closely at the few candidates afterwards with mhphotos_inbox_images.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return [cfg]
    if not item_ids or len(item_ids) > MAX_SHEET or len(set(item_ids)) != len(item_ids):
        return [{"error": f"Provide 1-{MAX_SHEET} distinct inbox item ids"}]
    items = await _inbox_items_by_id(cfg)
    if "_error" in items:
        return [items["_error"]]
    cell = max(160, min(cell, 480))
    previews: list[bytes | None] = []
    legend: list[dict[str, Any]] = []
    for number, iid in enumerate(item_ids, 1):
        raw = await _original(cfg, iid) if iid in items else None
        if not isinstance(raw, bytes):
            previews.append(None)
            legend.append({"n": number, "id": iid, "error": "not available"})
            continue
        previews.append(raw)
        c = _inbox_caption(items[iid])
        legend.append({"n": number, "id": iid, **{k: c[k] for k in ("filename", "rating", "status") if k in c}})
    sheet = await anyio.to_thread.run_sync(imaging.contact_sheet, previews, columns, cell)
    return [{"legend": legend}, Image(data=sheet, format="jpeg")]


@mcp.tool(structured_output=False)  # text + image blocks
async def mhphotos_edit_preview(
    inbox_item_id: str, adjust: dict[str, Any], size: int = 640
) -> list[Any]:
    """Show BEFORE and AFTER of a basic edit of an inbox photo (nothing is saved or sent).

    adjust: exposure (EV -2..2), contrast, saturation, warmth (-50..50 %), sharpen (0..100 %),
    crop (original|1:1|4:5|9:16|16:9). These are simple edits for the Instagram copy, not
    Lightroom-quality development; use the same dict in mhphotos_schedule_instagram.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return [cfg]
    clean, err = imaging.validate_adjust(adjust)
    if err or not clean:
        return [{"error": err or "adjust must not be empty"}]
    raw = await _original(cfg, inbox_item_id)
    if isinstance(raw, dict):
        return [raw]
    size = max(256, min(size, 1280))
    try:
        both = await anyio.to_thread.run_sync(
            lambda: imaging.before_after(raw, imaging.adjust(raw, clean), size)
        )
    except (OSError, ValueError):
        return [{"error": "image could not be decoded"}]
    return [{"id": inbox_item_id, "adjust": clean}, Image(data=both, format="jpeg")]


# ---------------------------------------------------------------- Instagram via the social queue
MIN_LEAD = timedelta(minutes=15)
MAX_LEAD = timedelta(days=60)


def _parse_when(value: str) -> datetime | dict[str, Any]:
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        return {"error": "scheduled_at must be ISO 8601 with time zone, e.g. 2026-10-20T18:00:00+02:00"}
    if when.tzinfo is None:
        return {"error": "scheduled_at needs a time zone offset, e.g. +02:00"}
    now = datetime.now(UTC)
    if when < now + MIN_LEAD:
        return {"error": "scheduled_at must be at least 15 minutes in the future (time to cancel)"}
    if when > now + MAX_LEAD:
        return {"error": "scheduled_at must be within the next 60 days"}
    return when


@mcp.tool()
async def mhphotos_schedule_instagram(
    caption: str,
    scheduled_at: str,
    photo_id: str | None = None,
    inbox_item_id: str | None = None,
    photo_ids: list[str] | None = None,
    accounts: str = "mhphotos",
    post_type: str = "post",
    adjust: dict[str, Any] | None = None,
    allow_duplicate: bool = False,
    confirm: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Queue a photo for Instagram in the MHPhotos social queue (it publishes automatically at that time).

    Source: either a Lightroom photo (photo_id, needs the Lightroom bridge and MHPHOTOS_EXPORT_DIR),
    a carousel of 2-10 Lightroom photos (photo_ids, in this order; post only) or an MHPhotos inbox
    item (inbox_item_id, works without Lightroom). accounts: mhphotos (default),
    malou, martin, or a comma list. post_type: post|story. adjust: optional basic edits for the
    Instagram copy only (see mhphotos_edit_preview). The MHPhotos duplicate check runs first; a photo
    that is already planned/posted is refused unless allow_duplicate=true. scheduled_at: ISO 8601 with
    offset, 15 minutes to 60 days ahead. Cancel in time with mhphotos_cancel_instagram. Dry run unless
    confirm=true; needs MHPHOTOS_ALLOW_INSTAGRAM=1 and the MHPhotos config.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    if not publishing.instagram_enabled():
        return {"error": publishing.instagram_disabled_message(), "code": "disabled"}
    if sum(bool(x) for x in (photo_id, inbox_item_id, photo_ids)) != 1:
        return {"error": "Give exactly one of photo_id, photo_ids (carousel) or inbox_item_id"}
    if photo_ids is not None:
        if not 2 <= len(photo_ids) <= 10 or len(set(photo_ids)) != len(photo_ids):
            return {"error": "photo_ids: 2-10 different Lightroom photos for a carousel"}
        if post_type != "post":
            return {"error": "a carousel is always post_type post"}
    lr_ids = photo_ids or ([photo_id] if photo_id else [])
    caption = caption.strip()
    if not caption or len(caption) > 2200:
        return {"error": "caption required (max 2200 characters)"}
    if post_type not in ("post", "story"):
        return {"error": "post_type must be post or story"}
    names = publishing.parse_accounts(accounts)
    if names is None:
        return {"error": f"accounts must be a comma list of: {', '.join(publishing.SOCIAL_ACCOUNTS)}"}
    edits, err = imaging.validate_adjust(adjust)
    if err:
        return {"error": err}
    when = _parse_when(scheduled_at)
    if isinstance(when, dict):
        return when
    dest = permissions.export_dir()
    label: str
    names_lr: list[str] = []
    if lr_ids:
        if dest is None:
            return {"error": permissions.export_disabled_message(), "code": "disabled"}
        for pid in lr_ids:
            meta = await _run(_adapter.get_photo, pid)
            if _is_error(meta):
                return meta
            names_lr.append(meta.filename)
        label = names_lr[0]
        look = await _run(_adapter.get_preview, lr_ids[0], 1024)  # for the duplicate check only
        if _is_error(look):
            return look
        original: bytes | None = None
    else:
        items = await _inbox_items_by_id(cfg)
        if "_error" in items:
            return items["_error"]
        if inbox_item_id not in items:
            return {"error": f"Inbox item not found: {inbox_item_id}"}
        label = str(items[inbox_item_id].get("originalFileName") or inbox_item_id)
        original = await _original(cfg, inbox_item_id)
        if isinstance(original, dict):
            return original
        look = original

    # MHPhotos duplicate check (best effort, like the app): same photo planned or posted already?
    dup: dict[str, Any] | None = None
    img_hash = ""
    try:
        thumb = await anyio.to_thread.run_sync(imaging.thumb_png_b64, look)
        code, resp = await anyio.to_thread.run_sync(publishing.social_duplicate, cfg, thumb, names[0])
        if code == 200:
            img_hash = str(resp.get("hash") or "")
            if resp.get("duplicate"):
                dup = {k: resp.get(k) for k in ("account_name", "scheduled_at", "status", "distance") if resp.get(k) is not None}
    except (OSError, ValueError):
        pass
    plan = {
        "photo": label,
        **({"carousel": names_lr} if photo_ids else {}),
        "accounts": names,
        "type": post_type,
        "scheduled_at": when.astimezone(UTC).isoformat(timespec="minutes"),
        "caption": caption,
        "edits": edits or None,
    }
    if dup and not allow_duplicate:
        return {"error": "This photo already looks planned or posted. Pass allow_duplicate=true to queue it anyway.",
                "code": "duplicate", "duplicate": dup, **plan}
    if not confirm:
        return {
            "dry_run": True, **plan, "duplicate": dup, "posted_automatically": True,
            "hint": "Nothing was queued. Call again with confirm=true; cancel later with mhphotos_cancel_instagram.",
        }
    approved, how = await gate.approve(
        ctx,
        f"AUTOMATISCH auf Instagram ({', '.join(names)}, {post_type}) am {when.astimezone().strftime('%d.%m.%Y %H:%M')} "
        f"veröffentlichen: {label}{f' (Karussell, {len(photo_ids)} Bilder)' if photo_ids else ''} – \"{caption[:80]}\"",
    )
    if not approved:
        return gate.refusal(how)
    originals: list[bytes] = []
    if lr_ids:
        batch_dir = Path(dest) / f"insta-{uuid.uuid4().hex[:8]}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        exported = await _run(_adapter.export_photos, lr_ids, str(batch_dir))
        if _is_error(exported):
            return exported
        for name in names_lr:
            path = batch_dir / f"{name.rsplit('.', 1)[0]}.jpg"
            if not path.is_file():
                return {"error": f"exported file not found: {path.name}"}
            originals.append(path.read_bytes())
    else:
        originals.append(original)
    try:
        def prepare(data: bytes) -> bytes:
            return imaging.instagram_jpeg(imaging.adjust(data, edits) if edits else data)

        prepared = [await anyio.to_thread.run_sync(prepare, data) for data in originals]
    except (OSError, ValueError) as exc:
        return {"error": f"image could not be prepared: {exc}"}
    final = prepared[0]
    extras = [(f"{Path(label).stem}-{i}.jpg", data) for i, data in enumerate(prepared[1:], start=1)]
    fields = {
        "accountId": ",".join(names), "type": post_type, "caption": caption,
        "scheduledAt": when.astimezone(UTC).isoformat(),
    }
    if img_hash:
        fields["imgHash"] = img_hash
    code, resp = await anyio.to_thread.run_sync(
        lambda: publishing.social_schedule(cfg, f"{Path(label).stem}.jpg", final, fields, extras=extras or None)
    )
    if code != 200 or not resp.get("ok"):
        return _app_error(code, resp)
    ids = resp.get("ids") or [resp.get("id")]
    change_id = audit.record(
        "mhphotos_schedule_instagram", lr_ids,
        {**plan, "queue_ids": ids, "inbox_item": inbox_item_id}, approved_by=how,
    )
    return {"applied": True, "change_id": change_id, "queue_ids": ids, **plan}


@mcp.tool()
async def mhphotos_instagram_queue(status: str | None = None, limit: int = 30) -> dict[str, Any]:
    """MHPhotos queue (newest first): scheduled, published and failed items.

    status filter: geplant | veroeffentlicht | fehler.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    code, data = await anyio.to_thread.run_sync(publishing.social_queue, cfg)
    if code != 200:
        return _app_error(code, data)
    rows = [r for r in data.get("items", []) if status is None or r.get("status") == status]
    rows.sort(key=lambda r: r.get("scheduled_at") or "", reverse=True)
    return {
        "items": [
            {
                "id": r.get("id"), "account": r.get("account_name") or r.get("account_id"),
                "type": r.get("type"), "status": r.get("status"), "scheduled_at": r.get("scheduled_at"),
                "caption": (r.get("caption") or "")[:120], "error": r.get("error"),
                "permalink": r.get("permalink"),
            }
            for r in rows[: max(1, min(limit, 100))]
        ]
    }


@mcp.tool()
async def mhphotos_site_photos(query: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Photos published on the MHPhotos homepage (newest first), e.g. to pick photos for a reel.

    query matches title, place, gallery or year (case-insensitive). Read-only.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    code, data = await anyio.to_thread.run_sync(publishing.site_photos, cfg)
    if code != 200:
        return _app_error(code, data)
    photos = data.get("photos") or []
    if query:
        q = query.lower().strip()
        photos = [p for p in photos if q in " ".join(str(v) for v in (
            p.get("title"), p.get("location"), p.get("year"), " ".join(p.get("categories") or []))).lower()]
    photos = sorted(photos, key=lambda p: str(p.get("uploadedAt") or ""), reverse=True)
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    page = photos[offset : offset + limit]
    out: dict[str, Any] = {"total": len(photos), "photos": [
        {k: p.get(k) for k in ("id", "title", "location", "year", "categories", "width", "height") if p.get(k)}
        for p in page]}
    if offset + len(page) < len(photos):
        out["next_offset"] = offset + len(page)
    return out


@mcp.tool()
async def mhphotos_reel_jobs(limit: int = 20) -> dict[str, Any]:
    """Reel jobs of the MHPhotos app: wartet -> rendering -> bereit (video ready) -> freigegeben. Read-only.

    The Hetzner reel renderer turns waiting jobs into silent 9:16 videos. Approving and scheduling a
    finished reel happens in the MHPhotos Content Studio; for music, download it there and post it in
    the Instagram app.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    code, data = await anyio.to_thread.run_sync(publishing.reel_jobs, cfg)
    if code != 200:
        return _app_error(code, data)
    jobs = data.get("jobs") or []
    return {"jobs": [
        {k: j.get(k) for k in ("id", "title", "status", "photoIds", "error", "createdAt", "updatedAt") if j.get(k)}
        | {"caption": (j.get("caption") or "")[:160]}
        for j in jobs[: max(1, min(limit, 100))]]}


@mcp.tool()
async def mhphotos_create_reel(
    photo_ids: list[str], title: str, caption: str, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Order a reel: a silent 9:16 slideshow of 3-12 homepage photos (ids from mhphotos_site_photos).

    Creates a reel job in the MHPhotos app; the Hetzner renderer makes the video (a few minutes).
    Nothing is published: approving and scheduling stay in the Content Studio. Order = photo order.
    Dry run unless confirm=true; needs MHPHOTOS_ALLOW_INSTAGRAM=1.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    if not publishing.instagram_enabled():
        return {"error": publishing.instagram_disabled_message(), "code": "disabled"}
    ids = [i.strip() for i in photo_ids if i and i.strip()]
    if not 3 <= len(ids) <= 12 or len(set(ids)) != len(ids):
        return {"error": "3-12 different homepage photo ids"}
    title, caption = title.strip()[:120], caption.strip()
    if not title or not caption or len(caption) > 2200:
        return {"error": "title and caption required (caption max 2200 characters)"}
    plan = {"photos": ids, "title": title, "caption": caption,
            "seconds": round(len(ids) * 3.2 - (len(ids) - 1) * 0.6, 1), "published": False}
    if not confirm:
        return {"dry_run": True, **plan, "hint": "Call again with confirm=true to order the reel."}
    approved, how = await gate.approve(ctx, f"Reel „{title}“ aus {len(ids)} Fotos rendern lassen (nichts wird veröffentlicht)")
    if not approved:
        return gate.refusal(how)
    code, data = await anyio.to_thread.run_sync(publishing.create_reel_job, cfg, ids, title, caption)
    if code != 200 or not data.get("ok"):
        return _app_error(code, data)
    job = data.get("job") or {}
    change_id = audit.record("mhphotos_create_reel", [], {**plan, "job_id": job.get("id")}, approved_by=how)
    return {"applied": True, "change_id": change_id, "job_id": job.get("id"), "status": job.get("status"), **plan,
            "next": "Check progress with mhphotos_reel_jobs; approve in the MHPhotos Content Studio."}


@mcp.tool()
async def mhphotos_best_times(account: str = "mhphotos") -> dict[str, Any]:
    """Best posting times of an Instagram account, from the MHPhotos social statistics. Read-only."""
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    if account not in publishing.SOCIAL_ACCOUNTS:
        return {"error": f"account must be one of: {', '.join(publishing.SOCIAL_ACCOUNTS)}"}
    code, data = await anyio.to_thread.run_sync(
        publishing.api, cfg, "GET", f"/api/admin/social-hub-best-time?accountId={account}")
    if code != 200:
        return _app_error(code, data)
    return {"account": account, **data}


@mcp.tool()
async def instagram_candidates(
    account: str = "mhphotos", captured_from: str | None = None, captured_to: str | None = None,
    min_rating: int = 4, count: int = 9, max_checks: int = 30,
) -> dict[str, Any]:
    """Good Lightroom photos that were never planned or posted on Instagram, for a weekly plan.

    Takes photos rated min_rating or more (default: the last three years), skips rejected ones and
    those with the keyword "Instagram", mixes them (a different mix every week) and asks the MHPhotos
    duplicate check for each until count photos are found that are neither planned nor posted on
    that account. Then: look at them (get_photo_previews), write captions in the style of earlier
    posts (mhphotos_instagram_posts), pick times (mhphotos_best_times, mhphotos_instagram_queue) and
    plan each with mhphotos_schedule_instagram (dry run first). Read-only.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    if account not in publishing.SOCIAL_ACCOUNTS:
        return {"error": f"account must be one of: {', '.join(publishing.SOCIAL_ACCOUNTS)}"}
    today = datetime.now().date()
    captured_to = captured_to or today.isoformat()
    captured_from = captured_from or today.replace(year=today.year - 3).isoformat()
    if bad := _check_range(captured_from, captured_to):
        return bad
    count, max_checks = max(1, min(count, 21)), max(1, min(max_checks, 60))
    recs = await _range(captured_from, captured_to)
    if isinstance(recs, dict):
        return recs
    pool = [r for r in recs if (r.get("r") or 0) >= min_rating and r.get("pick") != -1 and not r.get("insta")]
    week = today.isocalendar()
    random.Random(f"{account}-{week[0]}-{week[1]}").shuffle(pool)
    pool.sort(key=lambda r: -(r.get("r") or 0))  # stable: best first, shuffled within a rating
    found: list[dict[str, Any]] = []
    already, checked, unchecked = 0, 0, 0
    for r in pool:
        if len(found) >= count or checked >= max_checks:
            break
        checked += 1
        look = await _run(_adapter.get_preview, r["id"], 512)
        if _is_error(look):
            unchecked += 1
            continue
        try:
            thumb = await anyio.to_thread.run_sync(imaging.thumb_png_b64, look)
            code, resp = await anyio.to_thread.run_sync(publishing.social_duplicate, cfg, thumb, account)
        except (OSError, ValueError):
            code, resp = 0, {}
        if code == 200 and resp.get("duplicate"):
            already += 1
            continue
        item = _brief(r)
        if code != 200:
            item["duplicate_check"] = "not possible"
            unchecked += 1
        found.append(item)
    return {"account": account, "range": [captured_from, captured_to], "pool": len(pool),
            "checked": checked, "already_planned_or_posted": already, "candidates": found,
            **({"unchecked": unchecked} if unchecked else {})}


@mcp.tool()
async def mhphotos_cancel_instagram(
    queue_id: str, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Remove a not-yet-published item from the social queue (it will not be posted)."""
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    if not publishing.instagram_enabled():
        return {"error": publishing.instagram_disabled_message(), "code": "disabled"}
    if not publishing.valid_queue_id(queue_id):
        return {"error": "queue_id looks like q-1a2b3c4d; see mhphotos_instagram_queue"}
    code, data = await anyio.to_thread.run_sync(publishing.social_queue, cfg)
    if code != 200:
        return _app_error(code, data)
    item = next((r for r in data.get("items", []) if r.get("id") == queue_id), None)
    if item is None:
        return {"error": f"Queue item not found: {queue_id}. Use mhphotos_instagram_queue."}
    if item.get("status") == "veroeffentlicht":
        return {"error": "Already published on Instagram; delete the post in Instagram itself."}
    summary = {"id": queue_id, "status": item.get("status"), "scheduled_at": item.get("scheduled_at"),
               "account": item.get("account_name") or item.get("account_id"),
               "caption": (item.get("caption") or "")[:120]}
    if not confirm:
        return {"dry_run": True, **summary, "hint": "Call again with confirm=true to remove it from the queue."}
    approved, how = await gate.approve(ctx, f"Instagram-Beitrag aus der Warteschlange entfernen: {summary['caption'][:60]}")
    if not approved:
        return gate.refusal(how)
    code, resp = await anyio.to_thread.run_sync(publishing.social_cancel, cfg, queue_id)
    if code != 200:
        return _app_error(code, resp)
    change_id = audit.record("mhphotos_cancel_instagram", [], summary, approved_by=how)
    return {"applied": True, "change_id": change_id, **summary}


# ---------------------------------------------------------------- catalog insights (from the last catalog check)
SNAPSHOT_HINT = ("No catalog snapshot yet. Run catalog_audit_start without year (whole catalog, several minutes; "
                 "plug-in 0.1.26+ also records place, lens and focal length), then try again.")


def _snapshot() -> dict[str, Any]:
    snap = catalog_audit.load_snapshot()
    if not snap:
        return {"error": SNAPSHOT_HINT, "code": "no_snapshot"}
    return snap


def _scope_note(snap: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"checked_at": snap.get("finished_at")}
    if snap.get("year"):
        out["scope_note"] = (f"The last catalog check covered only {snap['year']}; run catalog_audit_start without "
                             "year for the whole catalog.")
    return out


def _page(items: list[Any], limit: int, offset: int, cap: int = 200) -> dict[str, Any]:
    limit, offset = max(1, min(limit, cap)), max(0, offset)
    page = items[offset : offset + limit]
    out: dict[str, Any] = {"total": len(items), "items": page}
    if offset + len(page) < len(items):
        out["next_offset"] = offset + len(page)
    return out


@mcp.tool()
async def catalog_audit_overview() -> dict[str, Any]:
    """Overview of the last catalog check per year: wrong folders, duplicates, non-photos, storage, open points.

    Best after a run over the whole catalog (catalog_audit_start without year); history lists when each scope
    (whole catalog or single years) was last checked. Read-only, no Lightroom call.
    """
    saved = catalog_audit.load_result()
    if not saved:
        return {"error": "No finished catalog check yet. Start one with catalog_audit_start."}
    snap = catalog_audit.load_snapshot()
    photos = snap["photos"] if snap and snap.get("finished_at") == saved.get("finished_at") else None
    out = catalog_insights.overview(saved, photos)
    out["history"] = {scope: h.get("finished_at") for scope, h in sorted(catalog_audit.load_history().items())}
    return out


@mcp.tool()
async def catalog_folder_moves(
    year: int | None = None, level: str | None = None, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """Photos in the wrong date folder, grouped by the folder they are in: the move list at folder level.

    Each entry: folder, photos_to_move, targets (expected folder path -> photos), levels, and whole_folder=true
    when every photo of the folder belongs into one target, so the folder can be dragged onto its parent (or
    renamed) in Lightroom's Folders panel in one step. year = target year, level = year|month|day|decade|no_day.
    Biggest folders first. Read-only, from the last catalog check.
    """
    saved = catalog_audit.load_result()
    if not saved:
        return {"error": "No finished catalog check yet. Start one with catalog_audit_start."}
    snap = catalog_audit.load_snapshot()
    moves = catalog_insights.folder_moves(saved, snap["photos"] if snap else None, year, level)
    out = {"checked_at": saved.get("finished_at"), "whole_folders": sum(1 for m in moves if m.get("whole_folder")),
           **_page(moves, limit, offset)}
    out["hint"] = ("whole_folder: drag the folder in Lightroom's Folders panel (never in Finder). Otherwise use "
                   "catalog_audit_collections(kind='wrong_folder') and drag the review collections day by day.")
    return out


@mcp.tool()
async def archive_candidates(
    min_age_years: int = 5, include_videos: bool = True, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """Photos nobody touched (no stars, flag, keywords, edits, colour label or virtual copies) and older than
    min_age_years: candidates to move to an archive drive, per folder (biggest first) and per year.

    whole_folder=true means every photo of the folder is a candidate, so the whole folder can be moved to the
    archive in Lightroom (Folders panel). rejected_any_age counts photos flagged as rejected. Read-only, from the
    catalog snapshot of the last catalog check.
    """
    if not 0 <= min_age_years <= 100:
        return {"error": "min_age_years must be 0-100"}
    snap = _snapshot()
    if _is_error(snap):
        return snap
    res = catalog_insights.archive_candidates(snap["photos"], min_age_years, include_videos=include_videos)
    folders = res.pop("folders")
    res.pop("_ids_by_year")
    res.pop("_rejected_ids")
    return {**_scope_note(snap), **res, "folders": _page(folders, limit, offset)}


@mcp.tool()
async def archive_candidates_collections(
    min_age_years: int = 5, include_videos: bool = True, include_rejected: bool = False,
    confirm: bool = False, ctx: Context | None = None,
) -> dict[str, Any]:
    """Put the archive candidates into review collections: "Archiv-Kandidaten › <year> › unberührt"
    (and "Archiv-Kandidaten › Abgelehnt › alle Jahre" with include_rejected). Moves and deletes nothing.

    Dry run unless confirm=true; needs operator opt-in.
    """
    if not 0 <= min_age_years <= 100:
        return {"error": "min_age_years must be 0-100"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    snap = _snapshot()
    if _is_error(snap):
        return snap
    res = catalog_insights.archive_candidates(snap["photos"], min_age_years, include_videos=include_videos)
    groups = [{"set": year, "name": "unberührt", "photo_ids": ids} for year, ids in sorted(res["_ids_by_year"].items())]
    if include_rejected and res["_rejected_ids"]:
        groups.append({"set": "Abgelehnt", "name": "alle Jahre", "photo_ids": res["_rejected_ids"]})
    return await _review_collections("Archiv-Kandidaten", groups, confirm, ctx, "archive_candidates_collections",
                                     {"min_age_years": min_age_years, "include_rejected": include_rejected})


async def _review_collections(root: str, groups: list[dict[str, Any]], confirm: bool, ctx: Context | None,
                              tool: str, change: dict[str, Any]) -> dict[str, Any]:
    """Shared flow for review collections below root: plan -> dry run -> approval -> fill in chunks -> log."""
    groups = [g for g in groups if g["photo_ids"]]
    photos = sum(len(g["photo_ids"]) for g in groups)
    plan = {"root": root, "collections": len(groups), "photos": photos,
            "first_collections": [f"{root} › {g['set']} › {g['name']} ({len(g['photo_ids'])})" for g in groups[:15]]}
    if not groups:
        return {**plan, "nothing_to_do": True}
    if not confirm:
        return {"dry_run": True, **plan, "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"{len(groups)} Sammlung(en) mit {photos} Fotos unter '{root}' anlegen")
    if not approved:
        return gate.refusal(how)
    added = missing = 0
    for call in catalog_audit.chunk_groups(groups):
        res = await _run(_adapter.create_review_collections, root, call)
        if _is_error(res):
            return {**res, "partly_done": {"photos_added": added}} if added else res
        added += sum(c["added"] for c in res["collections"])
        missing += int(res.get("missing") or 0)
    result = {"collections": len(groups), "photos_added": added, "photos_no_longer_in_catalog": missing}
    change_id = audit.record(tool, [], change, approved_by=how, result=result)
    return {"applied": True, "change_id": change_id, **plan, **result,
            "note": f"Additive; not undoable here. Delete the set '{root}' in Lightroom when you are done."}


# ---------------------------------------------------------------- pre-sorting (nightly)
@mcp.tool()
async def presort_status() -> dict[str, Any]:
    """Pre-sorting progress: months done, the month in progress, and the next months to do.

    Unreviewed = no stars and no flag (videos left out). The backlog per month comes from the catalog snapshot of
    the last catalog check; without one, give presort_month a month yourself. Read-only.
    """
    progress = presort.load_progress()
    snap = catalog_audit.load_snapshot()
    backlog = presort.unreviewed_by_month(snap["photos"]) if snap else None
    out: dict[str, Any] = {
        "months_done": sorted((m for m, e in progress.items() if e.get("done")), reverse=True),
        "progress": {m: e for m, e in sorted(progress.items(), reverse=True)[:24]},
    }
    if backlog is not None:
        out["unreviewed_total"] = sum(backlog.values())
        out["next"] = presort.next_months(progress, backlog)
        out["checked_at"] = snap.get("finished_at")
    else:
        out["note"] = SNAPSHOT_HINT
    return out


@mcp.tool()
async def presort_month(
    month: str, gap_seconds: int = 5, sheet_size: int = 6, batch_offset: int = 0, batches: int = 20
) -> dict[str, Any]:
    """Prepare pre-sorting of one month (YYYY-MM): unreviewed photos grouped into series and contact-sheet batches.

    Photos of the same camera taken at most gap_seconds apart form a series (bursts, repeated attempts). Each batch
    (sheet_size photos, default 6; a series is only split when it alone is bigger) is meant for one
    get_contact_sheet call (columns=6, cell=200, one call at a time); judge every photo, then call
    presort_apply with the ids: top (5★ candidate: could go on mhphotos.de / Instagram), good (4★), reject (blurred,
    wrong exposure, accidental), series_rest (the weaker shots of a series; keep the best one as top/good or leave
    it out). Owner's rules: family photos at most good, never top. Read-only.
    """
    rng = presort.month_range(month)
    if rng is None:
        return {"error": "month must be YYYY-MM"}
    if not 1 <= gap_seconds <= 120 or not 1 <= sheet_size <= imaging.MAX_SHEET_PHOTOS:
        return {"error": f"gap_seconds must be 1-120 and sheet_size 1-{imaging.MAX_SHEET_PHOTOS}"}
    recs = await _range(*rng)
    if isinstance(recs, dict):
        return recs
    plan = presort.plan(recs, month, gap_seconds, sheet_size)
    work = plan.pop("batches")
    offset, count = max(0, batch_offset), max(1, min(batches, 100))
    plan["batches"] = work[offset : offset + count]
    if offset + count < len(work):
        plan["next_batch_offset"] = offset + count
    done = presort.load_progress().get(month) or {}
    if done:
        plan["already_done"] = done
    plan["next_step"] = "get_contact_sheet(batch.photo_ids) per batch, then presort_apply(month, ..., batch=N)."
    return plan


@mcp.tool()
async def presort_apply(
    month: str, top: list[str] | None = None, good: list[str] | None = None, reject: list[str] | None = None,
    series_rest: list[str] | None = None, batch: int | None = None, finished: bool = False,
    confirm: bool = False, ctx: Context | None = None,
) -> dict[str, Any]:
    """Put pre-sorting verdicts into review collections "Vorsortieren › <month> › Kandidat 5★ / Kandidat 4★ /
    Aussortieren (Vorschlag) / Serie – Rest (Vorschlag)". Stars, flags and files are NOT changed; Martin decides
    in Lightroom. batch = number of the batch just judged (progress), finished=true marks the month as done.
    Additive. Dry run unless confirm=true; needs operator opt-in.
    """
    if presort.month_range(month) is None:
        return {"error": "month must be YYYY-MM"}
    verdicts = {"top": top or [], "good": good or [], "reject": reject or [], "series_rest": series_rest or []}
    ids = [i for v in verdicts.values() for i in v]
    if len(ids) != len(set(ids)):
        return {"error": "a photo may only appear in one verdict list"}
    if len(ids) > 5000:
        return {"error": "at most 5000 photos per call"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    groups = [{"set": month, "name": presort.VERDICTS[k], "photo_ids": v} for k, v in verdicts.items()]
    counts = {k: len(v) for k, v in verdicts.items()}
    if not ids:
        if finished and confirm:
            return {"applied": True, "progress": presort.record(month, counts, True, batch)}
        return {"nothing_to_do": True, "hint": "Give ids in top/good/reject/series_rest, or finished=true, confirm=true."}
    out = await _review_collections(presort.ROOT_SET, groups, confirm, ctx, "presort_apply",
                                    {"month": month, **counts, "batch": batch})
    if out.get("applied"):
        out["progress"] = presort.record(month, counts, finished, batch)
    return out


# ---------------------------------------------------------------- keywords
@mcp.tool()
async def keyword_check_start() -> dict[str, Any]:
    """Start a read-only check of all keywords (needs plug-in 0.1.26+): duplicates, variants, unused, loose ones.

    Runs in the background (counting photos per keyword takes a while on a big catalog); follow it with
    keyword_check_status, then keyword_check_results.
    """
    adapter = _adapter
    started, job = keyword_check.start(lambda offset, limit: adapter.keyword_tree(offset, limit, True))
    return {"started": started, **job.status()}


@mcp.tool()
async def keyword_check_status() -> dict[str, Any]:
    """Progress of the keyword check, or the counts of the last finished one."""
    job = keyword_check.current()
    out: dict[str, Any] = job.status() if job else {"status": "none"}
    if out["status"] in ("none", "done", "failed"):
        saved = keyword_check.load_result()
        if saved:
            out["last_result"] = {"finished_at": saved.get("finished_at"), "counts": saved.get("counts")}
    return out


@mcp.tool()
async def keyword_check_results(kind: str = "duplicates", limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Findings of the last keyword check, page by page.

    kind: duplicates (same name at the same place in the tree, or a loose top-level keyword next to the same one
    inside a hierarchy: keep + merge, ready for merge_keywords), variants (singular/plural or spelling: check first),
    same_name_elsewhere (the same name in different branches, e.g. two towns called Valencia: usually fine),
    unused (no photos, no children: Lightroom > Metadata > Purge Unused Keywords), loose (top-level keywords with
    photos, most used first: candidates to move into a hierarchy by drag and drop in the Keyword List).
    """
    if kind not in keyword_check.KINDS:
        return {"error": f"kind must be one of: {', '.join(keyword_check.KINDS)}"}
    saved = keyword_check.load_result()
    if not saved:
        return {"error": "No finished keyword check yet. Start one with keyword_check_start."}
    return {"kind": kind, "checked_at": saved.get("finished_at"), **_page(saved["findings"].get(kind, []), limit, offset)}


@mcp.tool()
async def merge_keywords(
    source_ids: list[str], target_id: str, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Merge keywords: every photo of the source keywords gets the target keyword and loses the source keyword.

    Ids come from keyword_check_results (keep.id = target, merge[].id = sources). The emptied source keywords stay
    in the list; remove them in Lightroom with Metadata > Purge Unused Keywords. Not undoable here (merge back by
    hand). Dry run unless confirm=true; needs operator opt-in and plug-in 0.1.26+.
    """
    sources = [s.strip() for s in source_ids if s and s.strip()]
    if not sources or len(sources) > 50 or len(set(sources)) != len(sources) or not target_id.strip():
        return {"error": "1-50 distinct source_ids and a target_id are required"}
    if target_id in sources:
        return {"error": "the target must not be one of the sources"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    saved = keyword_check.load_result() or {}
    known = {k["id"]: k for g in (saved.get("findings") or {}).get("duplicates", []) + (saved.get("findings") or {}).get("variants", [])
             for k in [g["keep"], *g["merge"]]}
    names = [known.get(i, {}).get("path") or i for i in sources]
    target = known.get(target_id, {}).get("path") or target_id
    if not confirm:
        return {"dry_run": True, "merge": names, "into": target,
                "photos_affected": sum(known.get(i, {}).get("photos") or 0 for i in sources) or None,
                "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"Stichwörter {', '.join(map(str, names))} in '{target}' zusammenführen")
    if not approved:
        return gate.refusal(how)
    moved, rounds, res = 0, 0, {}
    while rounds < 100:
        rounds += 1
        res = await _run(_adapter.merge_keywords, sources, target_id, 5000)
        if _is_error(res):
            return {**res, "partly_done": {"photos_moved": moved}} if moved else res
        moved += int(res.get("moved") or 0)
        if not res.get("remaining") or not res.get("moved"):
            break
    result = {"photos_moved": moved, "target": res.get("target") or target, "remaining": res.get("remaining") or 0}
    change_id = audit.record("merge_keywords", [], {"sources": names, "target": result["target"]}, approved_by=how,
                             result=result)
    return {"applied": True, "change_id": change_id, **result,
            "next_step": "Lightroom: Metadata > Purge Unused Keywords removes the emptied keywords."}


# ---------------------------------------------------------------- online on mhphotos.de / Instagram
async def _online_plan() -> dict[str, Any]:
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    code, data = await anyio.to_thread.run_sync(publishing.inbox_items, cfg)
    if code != 200:
        return _app_error(code, data)
    items = [i for i in data.get("items", []) if online_marks.wanted(i)]
    records: list[dict[str, Any]] = []
    for start, end in online_marks.days_needed(items):
        recs = await _range(start, end)
        if isinstance(recs, dict):
            return recs
        records += recs
    return {"items": len(items), **online_marks.match(items, records)}


@mcp.tool()
async def mark_online_in_lightroom(
    smart_collections: bool = True, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Mark in Lightroom which photos are online: keyword "MHPhotos-Online" for photos on mhphotos.de and
    "Instagram" for photos of Instagram posts, plus smart collections "MHPhotos › Online auf mhphotos.de" and
    "MHPhotos › Auf Instagram" (smart_collections=true) that fill themselves.

    Source: the MHPhotos Foto-Inbox (published items and items linked to an Instagram post), matched to Lightroom
    by Lightroom id (uploads since 0.1.26), else capture time + file name, file name ± 1 day, or capture time +
    camera. The dry run lists matches and items that could not be matched. Keywords are only added (safe to run
    again, e.g. weekly). Needs the MHPhotos app config and the operator opt-in for writes.
    """
    plan = await _online_plan()
    if _is_error(plan):
        return plan
    by_kw: dict[str, list[str]] = {}
    for m in plan["matches"]:
        for kw in m["keywords"]:
            if m["photo_id"] not in by_kw.setdefault(kw, []):
                by_kw[kw].append(m["photo_id"])
    summary = {"inbox_items_online": plan["items"], "matched": len(plan["matches"]),
               "not_matched": len(plan["unmatched"]), "photos_per_keyword": {k: len(v) for k, v in by_kw.items()}}
    if not confirm:
        return {"dry_run": True, **summary, "matches_sample": plan["matches"][:20],
                "not_matched_sample": plan["unmatched"][:20], "hint": "Call again with confirm=true."}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    approved, how = await gate.approve(
        ctx, f"Online-Markierung in Lightroom: {', '.join(f'{k} für {len(v)} Fotos' for k, v in by_kw.items()) or 'nichts'}")
    if not approved:
        return gate.refusal(how)
    tagged = 0
    for kw, ids in by_kw.items():
        for start in range(0, len(ids), MAX_BATCH):
            res = await _run(_adapter.add_keywords, ids[start:start + MAX_BATCH], [kw])
            if _is_error(res):
                return {**res, "partly_done": {"photos_tagged": tagged}}
            tagged += len(ids[start:start + MAX_BATCH])
    created = []
    if smart_collections:
        existing = await _run(_adapter.list_collections)
        names = set() if _is_error(existing) else {c.name for c in existing}
        for kw, name in online_marks.SMART_COLLECTIONS.items():
            if f"{online_marks.SMART_PARENT}/{name}" in names:
                continue
            res = await _run(_adapter.create_smart_collection, name, online_marks.SMART_PARENT,
                             PhotoFilter(keyword=kw).to_args())
            if not _is_error(res):
                created.append(f"{online_marks.SMART_PARENT}/{name}")
    result = {"photos_tagged": tagged, "smart_collections_created": created}
    change_id = audit.record("mark_online_in_lightroom", [], summary, approved_by=how, result=result)
    return {"applied": True, "change_id": change_id, **summary, **result}


# ---------------------------------------------------------------- gear statistics
async def _gear_list() -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    cfg = publishing.inbox_config()
    if cfg is None:
        return None, None
    code, data = await anyio.to_thread.run_sync(publishing.site_gear, cfg)
    if code != 200:
        return None, _app_error(code, data)
    return list(data.get("gear") or []), None


@mcp.tool()
async def gear_usage(year: int | None = None) -> dict[str, Any]:
    """Which cameras and lenses took the photos: photos, rated 4+ and 5★, first/last use, years, focal lengths.

    When the MHPhotos app is configured, the statistics are also matched to its gear list (Ausrüstung): matched
    items, EXIF names without a gear item, and active gear without any photo. year limits to one capture year.
    Read-only, from the catalog snapshot (lens and focal length need plug-in 0.1.26+).
    """
    if year is not None and not 1900 <= year <= 2200:
        return {"error": "year must be four digits"}
    snap = _snapshot()
    if _is_error(snap):
        return snap
    usage = catalog_insights.gear_usage(snap["photos"], year)
    out = {**_scope_note(snap), **usage}
    gear, err = await _gear_list()
    if gear is not None:
        out["mhphotos_gear"] = catalog_insights.match_gear(usage, gear)
    elif err:
        out["mhphotos_gear_error"] = err.get("error")
    if not any(p.get("lens") for p in snap["photos"][:5000]):
        out["note"] = "No lens data in the snapshot: install plug-in 0.1.26+ and run catalog_audit_start again."
    return out


@mcp.tool()
async def mhphotos_sync_gear_usage(confirm: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    """Send the gear statistics (per gear item: photos, rated 4+, 5★, first/last use, focal lengths) to the
    MHPhotos app, where the Ausrüstung admin page shows them. Admin data only, nothing becomes public.
    Dry run unless confirm=true.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    snap = _snapshot()
    if _is_error(snap):
        return snap
    gear, err = await _gear_list()
    if err:
        return err
    matched = catalog_insights.match_gear(catalog_insights.gear_usage(snap["photos"]), gear or [])
    payload = {"generatedAt": datetime.now(UTC).isoformat(timespec="seconds"), "checkedAt": snap.get("finished_at"),
               "items": {m["gear_id"]: {k: m[k] for k in ("photos", "rated_4_plus", "five_star", "first_used",
                                                          "last_used", "top_focal_lengths", "exif_names")}
                         for m in matched["matched"]},
               "unmatched": matched["unmatched"]}
    summary = {"gear_items": len(payload["items"]), "unmatched_cameras": len(matched["unmatched"]["cameras"]),
               "unmatched_lenses": len(matched["unmatched"]["lenses"]), "target": cfg.host}
    if not confirm:
        return {"dry_run": True, **summary, "matched": matched["matched"][:20], "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"Ausrüstungs-Statistik ({len(payload['items'])} Geräte) an {cfg.host} senden")
    if not approved:
        return gate.refusal(how)
    code, resp = await anyio.to_thread.run_sync(publishing.put_gear_usage, cfg, payload)
    if code != 200:
        return _app_error(code, resp)
    change_id = audit.record("mhphotos_sync_gear_usage", [], summary, approved_by=how)
    return {"applied": True, "change_id": change_id, **summary}


# ---------------------------------------------------------------- spots and own photos
async def _spot_matches(radius_m: int, min_rating: int, best: int = 5) -> dict[str, Any]:
    if not 25 <= radius_m <= 5000 or not 0 <= min_rating <= 5:
        return {"error": "radius_m must be 25-5000 and min_rating 0-5"}
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    snap = _snapshot()
    if _is_error(snap):
        return snap
    code, data = await anyio.to_thread.run_sync(publishing.site_spots, cfg)
    if code != 200:
        return _app_error(code, data)
    res = await anyio.to_thread.run_sync(
        lambda: catalog_insights.spot_matches(snap["photos"], list(data.get("spots") or []), radius_m, min_rating, best))
    return {**_scope_note(snap), **res}


@mcp.tool()
async def spot_photos(radius_m: int = 300, min_rating: int = 4, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Own photos at the MHPhotos spots: per spot the photos within radius_m (count, rated min_rating+, best ones),
    and the spots where no own photo rated min_rating+ exists yet (ideas for the photo planner).

    Read-only; uses the spot list of the MHPhotos app and the GPS positions of the catalog snapshot
    (plug-in 0.1.26+).
    """
    res = await _spot_matches(radius_m, min_rating)
    if _is_error(res):
        return res
    found = res.pop("spots_with_own_photos")
    missing = res.pop("spots_without_good_own_photo")
    res["spots_with_own_photos_count"] = len(found)
    res["spots_without_good_own_photo_count"] = len(missing)
    return {**res, "spots_with_own_photos": _page(found, limit, offset),
            "spots_without_good_own_photo": _page(missing, limit, 0)}


@mcp.tool()
async def spot_photos_collections(
    spot_ids: list[str], radius_m: int = 300, min_rating: int = 4, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Collections "MHPhotos-Spots › Spots › <spot name>" with the own photos rated min_rating+ at those spots
    (at most 50 spots per call). Additive. Dry run unless confirm=true; needs operator opt-in.
    """
    if not spot_ids or len(spot_ids) > 50:
        return {"error": "1-50 spot_ids"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    res = await _spot_matches(radius_m, min_rating, best=5000)
    if _is_error(res):
        return res
    wanted = set(spot_ids)
    groups = []
    for spot in res["spots_with_own_photos"]:
        if spot["spot_id"] in wanted:
            name = re.sub(r"[/\n\r]", "-", str(spot["name"]))[:120] or spot["spot_id"]
            ids = [b["id"] for b in spot.get("best", []) if b["rating"] >= min_rating]
            groups.append({"set": "Spots", "name": name, "photo_ids": ids})
    return await _review_collections("MHPhotos-Spots", groups, confirm, ctx, "spot_photos_collections",
                                     {"spots": len(groups), "radius_m": radius_m, "min_rating": min_rating})


@mcp.tool()
async def mhphotos_sync_spot_photos(
    radius_m: int = 300, min_rating: int = 4, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Send per spot how many own photos exist in Lightroom (count, rated min_rating+, best rating, last capture)
    to the MHPhotos app; the spot admin shows it and the photo planner can list spots without a good own photo.
    Admin data only, no images and nothing public. Dry run unless confirm=true.
    """
    res = await _spot_matches(radius_m, min_rating)
    if _is_error(res):
        return res
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    payload = {"generatedAt": datetime.now(UTC).isoformat(timespec="seconds"), "checkedAt": res.get("checked_at"),
               "radiusM": radius_m, "minRating": min_rating,
               "spots": {s["spot_id"]: {"photos": s["photos"], "rated": s["rated"], "bestRating": s.get("best_rating", 0),
                                        "lastCaptured": s.get("last_captured")} for s in res["spots_with_own_photos"]}}
    summary = {"spots_with_own_photos": len(payload["spots"]),
               "spots_without_good_own_photo": len(res["spots_without_good_own_photo"]), "target": cfg.host}
    if not confirm:
        return {"dry_run": True, **summary, "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"Eigene Fotos je Spot ({len(payload['spots'])} Spots) an {cfg.host} senden")
    if not approved:
        return gate.refusal(how)
    code, resp = await anyio.to_thread.run_sync(publishing.put_spot_photos, cfg, payload)
    if code != 200:
        return _app_error(code, resp)
    change_id = audit.record("mhphotos_sync_spot_photos", [], summary, approved_by=how)
    return {"applied": True, "change_id": change_id, **summary}


# ---------------------------------------------------------------- trip -> blog draft
async def _trip_records(captured_from: str, captured_to: str, collection_id: str | None) -> list[dict[str, Any]] | dict[str, Any]:
    if bad := _check_range(captured_from, captured_to):
        return bad
    recs = await _range(captured_from, captured_to)
    if isinstance(recs, dict) or not collection_id:
        return recs
    ids: set[str] = set()
    offset = 0
    while offset < 20000:
        res = await _run(_adapter.list_photos, collection_id, MAX_PAGE_SIZE, offset, None)
        if _is_error(res):
            return res
        photos, total = res
        ids.update(p.id for p in photos)
        offset += MAX_PAGE_SIZE
        if offset >= total:
            break
    return [r for r in recs if r["id"] in ids]


@mcp.tool()
async def trip_outline(
    captured_from: str, captured_to: str, collection_id: str | None = None, title: str | None = None,
    best_per_day: int = 3,
) -> dict[str, Any]:
    """A trip as a story: per day the places (in order), the route length from the photos' GPS, a map link and
    the best photos; plus a Markdown blog draft with placeholders. collection_id limits it to one collection
    (e.g. "Kreuzfahrt 2026"). Write the real text from it, then mhphotos_create_blog_draft. Read-only.
    """
    recs = await _trip_records(captured_from, captured_to, collection_id)
    if isinstance(recs, dict):
        return recs
    if not recs:
        return {"error": "No photos in that range (or collection)."}
    plan = trip_story.outline(recs, max(1, min(best_per_day, 10)))
    name = (title or "").strip() or f"Reise {captured_from} bis {captured_to}"
    return {**plan, "draft_markdown": trip_story.markdown(name, plan),
            "hint": "Send the best photos with send_to_mhphotos_inbox to have images for the post."}


@mcp.tool()
async def mhphotos_create_blog_draft(
    title: str, content: str | None = None, excerpt: str = "", tags: list[str] | None = None,
    captured_from: str | None = None, captured_to: str | None = None, collection_id: str | None = None,
    confirm: bool = False, ctx: Context | None = None,
) -> dict[str, Any]:
    """Save a blog post on mhphotos.de as an unpublished DRAFT (Admin › Blog), e.g. a travel report.

    content: the finished Markdown; without it, the draft from trip_outline(captured_from, captured_to,
    collection_id) is used. Nothing becomes public; Martin publishes in the admin. Dry run unless confirm=true.
    """
    title = title.strip()
    if not title or len(title) > 200:
        return {"error": "title required (max 200 characters)"}
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    if not content:
        if not captured_from or not captured_to:
            return {"error": "Give content, or captured_from and captured_to for a generated draft."}
        recs = await _trip_records(captured_from, captured_to, collection_id)
        if isinstance(recs, dict):
            return recs
        content = trip_story.markdown(title, trip_story.outline(recs))
    if len(content) > 200_000:
        return {"error": "content too long (max 200,000 characters)"}
    clean_tags = [t.strip() for t in (tags or []) if t and t.strip()][:20]
    post = {"title": title, "excerpt": excerpt.strip()[:500], "content": content, "tags": clean_tags}
    if not confirm:
        return {"dry_run": True, "target": cfg.host, "title": title, "characters": len(content), "tags": clean_tags,
                "preview": content[:1500], "hint": "Call again with confirm=true (saved as an unpublished draft)."}
    approved, how = await gate.approve(ctx, f"Blog-Entwurf '{title}' auf {cfg.host} speichern (nicht veröffentlicht)")
    if not approved:
        return gate.refusal(how)
    code, resp = await anyio.to_thread.run_sync(publishing.create_blog_draft, cfg, post)
    if code not in (200, 201):
        return _app_error(code, resp)
    saved = resp.get("post") or {}
    change_id = audit.record("mhphotos_create_blog_draft", [], {"title": title}, approved_by=how,
                             result={"post_id": saved.get("id")})
    return {"applied": True, "change_id": change_id, "post_id": saved.get("id"), "slug": saved.get("slug"),
            "published": False, "next_step": "Review and publish it in MHPhotos › Admin › Blog."}


# ---------------------------------------------------------------- duplicates, missing files, GPS gaps (0.1.27)
@mcp.tool()
async def duplicate_folders(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Double imports: the verified duplicates of the last catalog check per folder pair (copy folder -> folder
    with the original), biggest first. whole_folder=true: every photo of the copy folder is a duplicate, so the
    whole folder can be removed in Lightroom (Folders panel) after a look. Put the copies into review collections
    with catalog_audit_collections(kind="duplicates"). Read-only.
    """
    saved = catalog_audit.load_result()
    if not saved:
        return {"error": SNAPSHOT_HINT, "code": "no_result"}
    snap = catalog_audit.load_snapshot()
    rows = catalog_insights.duplicate_folders(saved, (snap or {}).get("photos"))
    for r in rows:
        r.pop("photo_ids")
    return {"checked_at": saved.get("finished_at"), "copy_folders": len({r["copy_folder"] for r in rows}),
            "whole_folders": sum(1 for r in rows if r.get("whole_folder")),
            "photos": sum(r["photos"] for r in rows), "gb": round(sum(r["gb"] for r in rows), 2),
            "pairs": _page(rows, limit, offset)}


@mcp.tool()
async def missing_files_by_drive() -> dict[str, Any]:
    """Photos whose original file Lightroom cannot find, per drive and folder: offline (drive not connected,
    nothing lost) or missing (drive connected, file gone or moved outside Lightroom). Needs a catalog check with
    check_files=true. Read-only.
    """
    saved = catalog_audit.load_result()
    if not saved:
        return {"error": SNAPSHOT_HINT, "code": "no_result"}
    snap = catalog_audit.load_snapshot()
    res = await anyio.to_thread.run_sync(
        lambda: catalog_insights.missing_by_drive(saved, (snap or {}).get("photos"), os.path.isdir))
    for d in res["drives"]:
        d.pop("photo_ids")
        d["folders"] = d["folders"][:30]
    if not res["checked_files"]:
        res["hint"] = "The last check did not look at the files: run catalog_audit_start(check_files=true)."
    return {"checked_at": saved.get("finished_at"), **res}


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _gps_args(captured_from: str | None, captured_to: str | None, window_minutes: int) -> str | None:
    if any(d and not _DAY.match(d) for d in (captured_from, captured_to)):
        return "captured_from/captured_to must be YYYY-MM-DD"
    if not 1 <= window_minutes <= 180:
        return "window_minutes must be 1-180"
    return None


@mcp.tool()
async def gps_gaps(captured_from: str | None = None, captured_to: str | None = None, window_minutes: int = 30,
                   limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Photos without GPS that were taken at most window_minutes before or after a photo with GPS (any camera,
    e.g. the phone): each gets that photo's position as a suggestion, with the distance in minutes. by_month
    shows where most gaps can be closed. Write them with fill_gps_gaps. Read-only, from the catalog snapshot.
    """
    if err := _gps_args(captured_from, captured_to, window_minutes):
        return {"error": err}
    snap = _snapshot()
    if _is_error(snap):
        return snap
    res = await anyio.to_thread.run_sync(
        lambda: catalog_insights.gps_gaps(snap["photos"], window_minutes, captured_from, captured_to))
    items = res.pop("suggestions")
    return {**_scope_note(snap), **res, "suggestions": _page(items, limit, offset)}


MAX_GPS_FILL = 5000


@mcp.tool()
async def fill_gps_gaps(captured_from: str, captured_to: str, window_minutes: int = 30, max_photos: int = 2000,
                        confirm: bool = False, ctx: Context | None = None) -> dict[str, Any]:
    """Write the GPS suggestions of gps_gaps for one date range (at most max_photos, up to 5000): every photo
    gets the position of the nearest photo with GPS taken within window_minutes. Only photos without GPS are
    touched; place names are not changed. Check samples with gps_gaps first.

    Dry run unless confirm=true; needs operator opt-in.
    """
    if err := _gps_args(captured_from, captured_to, window_minutes):
        return {"error": err}
    if not 1 <= max_photos <= MAX_GPS_FILL:
        return {"error": f"max_photos must be 1-{MAX_GPS_FILL}"}
    snap = _snapshot()
    if _is_error(snap):
        return snap
    res = await anyio.to_thread.run_sync(
        lambda: catalog_insights.gps_gaps(snap["photos"], window_minutes, captured_from, captured_to))
    todo = res["suggestions"][:max_photos]
    groups = catalog_insights.gps_groups(todo)
    plan = {"captured_from": captured_from, "captured_to": captured_to, "window_minutes": window_minutes,
            "photos": len(todo), "positions": len(groups), "more_left": max(0, res["suggestions_total"] - len(todo))}
    if not todo:
        return {**plan, "nothing_to_do": True}
    if not confirm:
        return {"dry_run": True, **plan, "sample": todo[:10], "hint": "Call again with confirm=true."}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    approved, how = await gate.approve(ctx, f"GPS-Position für {len(todo)} Fotos ({captured_from} bis {captured_to}) "
                                            "aus Fotos derselben Zeit übernehmen")
    if not approved:
        return gate.refusal(how)
    written, gps_error = 0, None
    for g in groups:
        for start in range(0, len(g["photo_ids"]), 200):
            part = g["photo_ids"][start:start + 200]
            out = await _run(_adapter.set_location, part, g["lat"], g["lon"], {})
            if _is_error(out):
                return {**out, "partly_done": {"photos_written": written}} if written else out
            if out.get("gps_written") is False:
                gps_error = out.get("gps_error") or "this Lightroom version does not allow writing GPS"
                break
            written += int(out.get("updated") or 0)
        if gps_error:
            break
    result = {"photos_written": written, **({"gps_error": gps_error} if gps_error else {})}
    change_id = audit.record("fill_gps_gaps", [s["id"] for s in todo[:written]], {"gps_from_neighbours": plan},
                             approved_by=how, result=result)
    return {"applied": not gps_error, "change_id": change_id, **plan, **result,
            "note": "The snapshot still shows the old state until the next catalog check."}


# ---------------------------------------------------------------- print candidates, calendar (0.1.27)
def _snapshot_photos() -> list[dict[str, Any]] | dict[str, Any]:
    snap = _snapshot()
    if _is_error(snap):
        return snap
    photos = snap["photos"]
    if photos and not any(p.get("w") or p.get("cw") for p in photos[:5000]):
        return {"error": "The catalog snapshot has no image sizes yet (plug-in 0.1.27+): run catalog_audit_start "
                         "without year again.", "code": "old_snapshot"}
    return photos


@mcp.tool()
async def print_candidates(min_rating: int = 4, min_size: str = "45×30", limit: int = 50,
                           offset: int = 0) -> dict[str, Any]:
    """Photos good enough for a wall print: rated min_rating+, not rejected, with enough pixels after cropping
    for at least min_size cm (30×20, 45×30, 60×40, 90×60, 120×80 at 150 dpi), grouped by the biggest size they
    allow, best first. Read-only, from the catalog snapshot (plug-in 0.1.27+).
    """
    if not 1 <= min_rating <= 5:
        return {"error": "min_rating must be 1-5"}
    photos = _snapshot_photos()
    if _is_error(photos):
        return photos
    try:
        res = catalog_insights.print_candidates(photos, min_rating, min_size)
    except ValueError as exc:
        return {"error": str(exc)}
    for g in res["groups"]:
        g["items"] = _page(g["items"], limit, offset)
    return res


@mcp.tool()
async def print_candidates_collections(min_rating: int = 4, min_size: str = "45×30", confirm: bool = False,
                                       ctx: Context | None = None) -> dict[str, Any]:
    """Review collections "Druck-Kandidaten › <max size> › <stars>★" with the print candidates. Additive.

    Dry run unless confirm=true; needs operator opt-in.
    """
    if not 1 <= min_rating <= 5:
        return {"error": "min_rating must be 1-5"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    photos = _snapshot_photos()
    if _is_error(photos):
        return photos
    try:
        res = catalog_insights.print_candidates(photos, min_rating, min_size)
    except ValueError as exc:
        return {"error": str(exc)}
    groups = []
    for g in res["groups"]:
        for stars in range(5, min_rating - 1, -1):
            ids = [i["id"] for i in g["items"] if i.get("r") == stars]
            groups.append({"set": f"bis {g['max_size']} cm", "name": f"{stars}★", "photo_ids": ids})
    return await _review_collections("Druck-Kandidaten", groups, confirm, ctx, "print_candidates_collections",
                                     {"min_rating": min_rating, "min_size": min_size})


def _calendar_args(year: int, per_month: int, min_rating: int) -> str | None:
    if not 2000 <= year <= 2100:
        return "year must be 2000-2100"
    if not 1 <= per_month <= 30:
        return "per_month must be 1-30"
    if not 1 <= min_rating <= 5:
        return "min_rating must be 1-5"
    return None


@mcp.tool()
async def calendar_candidates(year: int = 2027, per_month: int = 8, min_rating: int = 4,
                              orientation: str = "landscape", min_size: str = "45×30") -> dict[str, Any]:
    """Suggestions for a photo calendar: for each of the 12 months the best photos taken in that month (any
    year), rated min_rating+, in the wanted orientation (landscape, portrait, any) and big enough for min_size
    (an A3 calendar needs about 45×30). Variety: one photo per day, at most two per year and place where
    possible. Look at them with get_contact_sheet (6 per sheet), then calendar_collections. Read-only.
    """
    if err := _calendar_args(year, per_month, min_rating):
        return {"error": err}
    photos = _snapshot_photos()
    if _is_error(photos):
        return photos
    try:
        res = catalog_insights.calendar_candidates(photos, per_month, min_rating, orientation, min_size)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"calendar": year, **res}


@mcp.tool()
async def calendar_collections(year: int = 2027, per_month: int = 8, min_rating: int = 4,
                               orientation: str = "landscape", min_size: str = "45×30",
                               photo_ids_by_month: dict[str, list[str]] | None = None, confirm: bool = False,
                               ctx: Context | None = None) -> dict[str, Any]:
    """Collections "Kalender <year> › Vorschläge › 01 Januar" … "12 Dezember" with the calendar suggestions, or
    with your own pick per month (photo_ids_by_month, keys "1"-"12"). Additive; pick the final photo per month in
    Lightroom. Dry run unless confirm=true; needs operator opt-in.
    """
    if err := _calendar_args(year, per_month, min_rating):
        return {"error": err}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    names = catalog_insights.MONTHS_DE
    if photo_ids_by_month:
        if any(k not in {str(m) for m in range(1, 13)} for k in photo_ids_by_month) or \
                any(not isinstance(v, list) or len(v) > 100 for v in photo_ids_by_month.values()):
            return {"error": "photo_ids_by_month: keys '1'-'12', at most 100 ids each"}
        by_month = {int(k): list(v) for k, v in photo_ids_by_month.items()}
    else:
        photos = _snapshot_photos()
        if _is_error(photos):
            return photos
        try:
            res = catalog_insights.calendar_candidates(photos, per_month, min_rating, orientation, min_size)
        except ValueError as exc:
            return {"error": str(exc)}
        by_month = {m["month"]: [s["id"] for s in m["suggestions"]] for m in res["months"]}
    groups = [{"set": "Vorschläge", "name": f"{m:02d} {names[m - 1]}", "photo_ids": ids}
              for m, ids in sorted(by_month.items())]
    return await _review_collections(f"Kalender {year}", groups, confirm, ctx, "calendar_collections",
                                     {"year": year, "own_pick": bool(photo_ids_by_month)})


# ---------------------------------------------------------------- weekly report (0.1.27)
def _item_time(item: dict[str, Any]) -> str:
    for key in ("publishedAt", "approvedAt", "updatedAt", "createdAt", "uploadedAt"):
        if item.get(key):
            return str(item[key])
    return ""


@mcp.tool()
async def weekly_report(days: int = 7) -> dict[str, Any]:
    """A short report for the last days (default 7): new photos in Lightroom (by capture date) and how many are
    still unreviewed, the unreviewed backlog of the whole catalog, what went online on mhphotos.de and Instagram,
    and the spots without a good own photo. Each part that cannot be read says why. Read-only.
    """
    if not 1 <= days <= 62:
        return {"error": "days must be 1-62"}
    today = datetime.now().date()
    since = today - timedelta(days=days - 1)
    out: dict[str, Any] = {"from": since.isoformat(), "to": today.isoformat()}
    recs: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await _run(_adapter.photos_by_date, since.isoformat(), today.isoformat(), offset, 2000, offset == 0)
        if _is_error(page):
            out["new_photos"] = {"error": page.get("error")}
            break
        recs += page.get("photos") or []
        nxt = int(page.get("next_offset") or 0)
        if nxt <= offset or nxt >= int(page.get("total") or 0):
            break
        offset = nxt
    if "new_photos" not in out:
        photos = [r for r in recs if not presort.is_video(r)]
        per_day: dict[str, int] = {}
        for r in photos:
            per_day[str(r.get("t") or "")[:10]] = per_day.get(str(r.get("t") or "")[:10], 0) + 1
        out["new_photos"] = {"photos": len(photos), "videos": len(recs) - len(photos),
                             "unreviewed": sum(1 for r in photos if presort.is_unreviewed(r)),
                             "rated_4_plus": sum(1 for r in photos if (r.get("r") or 0) >= 4),
                             "busiest_days": sorted(per_day.items(), key=lambda x: -x[1])[:5]}
    snap = catalog_audit.load_snapshot()
    if snap:
        backlog = presort.unreviewed_by_month(snap["photos"])
        out["backlog"] = {"unreviewed_total": sum(backlog.values()), "as_of": snap.get("finished_at"),
                          "next_months": presort.next_months(presort.load_progress(), backlog, 3)}
    else:
        out["backlog"] = {"error": SNAPSHOT_HINT}
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        out["online"] = {"error": cfg.get("error")}
    else:
        code, data = await anyio.to_thread.run_sync(publishing.inbox_items, cfg)
        if code != 200:
            out["online"] = _app_error(code, data)
        else:
            recent = [i for i in data.get("items", []) if _item_time(i)[:10] >= since.isoformat()]
            out["online"] = {"inbox_new": len(recent),
                             "on_homepage": sum(1 for i in recent if i.get("status") == "uebernommen"),
                             "on_instagram": sum(1 for i in recent if i.get("instagramId")),
                             "waiting": sum(1 for i in data.get("items", []) if i.get("status") == "wartet")}
        spots = await _spot_matches(300, 4)
        if _is_error(spots):
            out["spots"] = {"error": spots.get("error")}
        else:
            missing = spots.get("spots_without_good_own_photo") or []
            out["spots"] = {"without_good_own_photo": len(missing),
                            "ideas": [s.get("name") for s in missing[:5]]}
    return out


# ---------------------------------------------------------------- toolsets
# MHPHOTOS_TOOLSETS="read,develop" limits which tools are offered (fewer tools = fewer tokens,
# smaller blast radius). Default: all. "read" is always on.
TOOLSETS: dict[str, set[str]] = {
    "read": {
        "connection_status", "lightroom_selftest", "list_collections", "list_photos",
        "get_photo_metadata", "get_photo_previews", "get_contact_sheet", "get_develop_settings",
        "list_presets", "backlog_report", "list_changes",
        "catalog_audit_start", "catalog_audit_status", "catalog_audit_results", "catalog_audit_cancel",
        "photos_by_date", "suggest_locations", "list_webhooks",
        "catalog_audit_overview", "catalog_folder_moves", "archive_candidates", "presort_status", "presort_month",
        "keyword_check_start", "keyword_check_status", "keyword_check_results", "gear_usage", "trip_outline",
        "duplicate_folders", "missing_files_by_drive", "gps_gaps", "print_candidates", "calendar_candidates",
        "weekly_report",
    },
    "edit": {
        "set_rating", "set_flag", "add_keywords", "set_color_label", "set_title_caption",
        "create_collection", "move_collection", "create_smart_collection", "create_keyword", "import_photo",
        "set_custom_metadata", "send_webhook", "add_to_collection", "export_photos", "undo_last_change",
        "catalog_audit_collections", "set_location", "rotate_photos", "write_name_dates_to_files",
        "archive_candidates_collections", "presort_apply", "merge_keywords", "mark_online_in_lightroom",
        "spot_photos_collections", "fill_gps_gaps", "print_candidates_collections", "calendar_collections",
    },
    "develop": {
        "apply_preset", "apply_develop_settings", "create_preset", "sync_develop_settings",
        "create_virtual_copies", "virtual_copy_job", "crop_to_aspect", "build_smart_previews", "auto_tone",
    },
    "mhphotos": {
        "send_to_mhphotos_inbox", "mhphotos_galleries", "mhphotos_inbox", "mhphotos_analyze_inbox",
        "mhphotos_publish", "mhphotos_instagram_posts", "mhphotos_instagram_images",
        "mhphotos_schedule_instagram", "mhphotos_instagram_queue", "mhphotos_cancel_instagram",
        "mhphotos_inbox_images", "mhphotos_inbox_contact_sheet", "mhphotos_edit_preview",
        "mhphotos_best_times", "instagram_candidates", "mhphotos_site_photos", "mhphotos_reel_jobs",
        "mhphotos_create_reel", "spot_photos", "mhphotos_sync_gear_usage", "mhphotos_sync_spot_photos",
        "mhphotos_create_blog_draft",
    },
}


def apply_toolsets(spec: str | None) -> list[str]:
    """Remove every tool outside the selected toolsets. Returns the enabled set names."""
    wanted = [w.strip().lower() for w in (spec or "all").split(",") if w.strip()] or ["all"]
    unknown = [w for w in wanted if w != "all" and w not in TOOLSETS]
    if unknown:
        raise ValueError(
            f"Unknown toolset(s): {', '.join(unknown)}. Choose from: all, {', '.join(TOOLSETS)}"
        )
    if "all" in wanted:
        return list(TOOLSETS)
    enabled = {"read", *wanted}
    keep = set().union(*(TOOLSETS[name] for name in enabled))
    for name in set().union(*TOOLSETS.values()) - keep:
        try:
            mcp.remove_tool(name)
        except Exception:  # noqa: BLE001,S110 (tool may not exist in this build)
            pass
    return sorted(enabled)


# ---------------------------------------------------------------- startup
def main() -> None:
    """Run the MCP server over stdio, or a helper command (doctor, install-plugin)."""
    if len(sys.argv) > 1:
        from . import cli

        sys.exit(cli.run(sys.argv[1:]))
    try:
        apply_toolsets(os.environ.get("MHPHOTOS_TOOLSETS"))
    except ValueError as exc:
        sys.exit(str(exc))
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

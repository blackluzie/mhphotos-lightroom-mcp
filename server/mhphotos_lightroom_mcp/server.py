"""MCP server: read tools always on, edit/export tools behind a permission gate.

Adapter is chosen with MHPHOTOS_ADAPTER=mock (default) | bridge.
"""

import dataclasses
import json
import os
import re
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import Context, FastMCP, Image

from . import audit, develop, gate, imaging, permissions, publishing
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
    name: str, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Create a collection (album), e.g. for picked photos. Returns the existing one if the name exists.

    Dry run unless confirm=true; needs operator opt-in. Not undoable (delete it in Lightroom).
    """
    name = name.strip()
    if not name or len(name) > 120:
        return {"error": "name required (max 120 characters)"}
    if not permissions.writes_enabled():
        return {"error": permissions.writes_disabled_message(), "code": "disabled"}
    existing = await _run(_adapter.list_collections)
    if _is_error(existing):
        return existing
    same = [c for c in existing if c.name == name]
    if same:
        return {"exists": True, "id": same[0].id, "name": name, "hint": "Nothing to create; use this id."}
    if not confirm:
        return {"dry_run": True, "would_create": name, "hint": "Call again with confirm=true."}
    approved, how = await gate.approve(ctx, f"Sammlung '{name}' in Lightroom anlegen")
    if not approved:
        return gate.refusal(how)
    result = await _run(_adapter.create_collection, name)
    if _is_error(result):
        return result
    change_id = audit.record("create_collection", [], {"name": name}, approved_by=how, result=result)
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
    photo_ids: list[str], confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Create virtual copies (max 10) so edits can be tried without touching the originals.

    Apply presets/settings to the returned copy ids, compare with get_photo_previews, keep or
    delete the copy in Lightroom. Lightroom only copies the selected photos, so the plugin
    switches to the Library grid and "All Photographs" and changes your selection. Needs opt-in.
    """
    if len(photo_ids) > 10:
        return {"error": "At most 10 virtual copies per call"}
    return await _edit(
        photo_ids, {"virtual_copies": len(photo_ids)}, lambda: _adapter.create_virtual_copies(photo_ids),
        confirm, ctx=ctx, tool="create_virtual_copies",
        summary="Virtuelle Kopien anlegen (Auswahl in Lightroom ändert sich)",
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


@mcp.tool()
async def export_photos(
    photo_ids: list[str], confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
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
        names.append({"id": pid, "filename": meta.filename})
    if not confirm:
        return {
            "dry_run": True,
            "destination": dest,
            "files": [n["filename"] for n in names],
            "hint": "Nothing was exported. Call again with confirm=true to export.",
        }
    approved, how = await gate.approve(ctx, f"{len(names)} Foto(s) als JPEG nach {dest} exportieren: {_names(names)}")
    if not approved:
        return gate.refusal(how)
    Path(dest).mkdir(parents=True, exist_ok=True)
    result = await _run(_adapter.export_photos, photo_ids, dest)
    if _is_error(result):
        return result
    change_id = audit.record("export_photos", photo_ids, {"destination": dest}, approved_by=how)
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

    It appears under the plugin's own preset folder. Dry run unless confirm=true; needs
    operator opt-in. Creating a preset changes no photo.
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
    change_id = audit.record("create_preset", [], {"name": name.strip(), "settings": clean}, approved_by=how)
    return {"applied": True, "change_id": change_id, **result}


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


# ---------------------------------------------------------------- Instagram via the SocialHub
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
    accounts: str = "mhphotos",
    post_type: str = "post",
    adjust: dict[str, Any] | None = None,
    allow_duplicate: bool = False,
    confirm: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Queue a photo for Instagram in the MHPhotos SocialHub (it publishes automatically at that time).

    Source: either a Lightroom photo (photo_id, needs the Lightroom bridge and MHPHOTOS_EXPORT_DIR) or
    an MHPhotos inbox item (inbox_item_id, works without Lightroom). accounts: mhphotos (default),
    malou, martin, or a comma list. post_type: post|story. adjust: optional basic edits for the
    Instagram copy only (see mhphotos_edit_preview). The SocialHub duplicate check runs first; a photo
    that is already planned/posted is refused unless allow_duplicate=true. scheduled_at: ISO 8601 with
    offset, 15 minutes to 60 days ahead. Cancel in time with mhphotos_cancel_instagram. Dry run unless
    confirm=true; needs MHPHOTOS_ALLOW_INSTAGRAM=1 and the MHPhotos config.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    if not publishing.instagram_enabled():
        return {"error": publishing.instagram_disabled_message(), "code": "disabled"}
    if bool(photo_id) == bool(inbox_item_id):
        return {"error": "Give exactly one of photo_id (Lightroom) or inbox_item_id (MHPhotos inbox)"}
    caption = caption.strip()
    if not caption or len(caption) > 2200:
        return {"error": "caption required (max 2200 characters)"}
    if post_type not in ("post", "story"):
        return {"error": "post_type must be post or story"}
    names = publishing.parse_accounts(accounts)
    if names is None:
        return {"error": f"accounts must be a comma list of: {', '.join(publishing.HUB_ACCOUNTS)}"}
    edits, err = imaging.validate_adjust(adjust)
    if err:
        return {"error": err}
    when = _parse_when(scheduled_at)
    if isinstance(when, dict):
        return when
    dest = permissions.export_dir()
    label: str
    if photo_id:
        if dest is None:
            return {"error": permissions.export_disabled_message(), "code": "disabled"}
        meta = await _run(_adapter.get_photo, photo_id)
        if _is_error(meta):
            return meta
        label = meta.filename
        look = await _run(_adapter.get_preview, photo_id, 1024)  # for the duplicate check only
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

    # SocialHub duplicate check (best effort, like the app): same photo planned or posted already?
    dup: dict[str, Any] | None = None
    img_hash = ""
    try:
        thumb = await anyio.to_thread.run_sync(imaging.thumb_png_b64, look)
        code, resp = await anyio.to_thread.run_sync(publishing.hub_duplicate, cfg, thumb, names[0])
        if code == 200:
            img_hash = str(resp.get("hash") or "")
            if resp.get("duplicate"):
                dup = {k: resp.get(k) for k in ("account_name", "scheduled_at", "status", "distance") if resp.get(k) is not None}
    except (OSError, ValueError):
        pass
    plan = {
        "photo": label,
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
        f"veröffentlichen: {label} – \"{caption[:80]}\"",
    )
    if not approved:
        return gate.refusal(how)
    if photo_id:
        batch_dir = Path(dest) / f"insta-{uuid.uuid4().hex[:8]}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        exported = await _run(_adapter.export_photos, [photo_id], str(batch_dir))
        if _is_error(exported):
            return exported
        path = batch_dir / f"{label.rsplit('.', 1)[0]}.jpg"
        if not path.is_file():
            return {"error": "exported file not found"}
        original = path.read_bytes()
    try:
        def prepare() -> bytes:
            data = imaging.adjust(original, edits) if edits else original
            return imaging.instagram_jpeg(data)

        final = await anyio.to_thread.run_sync(prepare)
    except (OSError, ValueError) as exc:
        return {"error": f"image could not be prepared: {exc}"}
    fields = {
        "accountId": ",".join(names), "type": post_type, "caption": caption,
        "scheduledAt": when.astimezone(UTC).isoformat(),
    }
    if img_hash:
        fields["imgHash"] = img_hash
    code, resp = await anyio.to_thread.run_sync(
        publishing.hub_schedule, cfg, f"{Path(label).stem}.jpg", final, fields
    )
    if code != 200 or not resp.get("ok"):
        return _app_error(code, resp)
    ids = resp.get("ids") or [resp.get("id")]
    change_id = audit.record(
        "mhphotos_schedule_instagram", [photo_id] if photo_id else [],
        {**plan, "queue_ids": ids, "inbox_item": inbox_item_id}, approved_by=how,
    )
    return {"applied": True, "change_id": change_id, "queue_ids": ids, **plan}


@mcp.tool()
async def mhphotos_instagram_queue(status: str | None = None, limit: int = 30) -> dict[str, Any]:
    """The SocialHub queue (newest first): scheduled, published and failed items.

    status filter: geplant | veroeffentlicht | fehler.
    """
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    code, data = await anyio.to_thread.run_sync(publishing.hub_queue, cfg)
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
async def mhphotos_cancel_instagram(
    queue_id: str, confirm: bool = False, ctx: Context | None = None
) -> dict[str, Any]:
    """Remove a not-yet-published item from the SocialHub queue (it will not be posted)."""
    cfg = _app_cfg()
    if isinstance(cfg, dict):
        return cfg
    if not publishing.instagram_enabled():
        return {"error": publishing.instagram_disabled_message(), "code": "disabled"}
    if not publishing.valid_queue_id(queue_id):
        return {"error": "queue_id looks like q-1a2b3c4d; see mhphotos_instagram_queue"}
    code, data = await anyio.to_thread.run_sync(publishing.hub_queue, cfg)
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
    code, resp = await anyio.to_thread.run_sync(publishing.hub_cancel, cfg, queue_id)
    if code != 200:
        return _app_error(code, resp)
    change_id = audit.record("mhphotos_cancel_instagram", [], summary, approved_by=how)
    return {"applied": True, "change_id": change_id, **summary}


# ---------------------------------------------------------------- toolsets
# MHPHOTOS_TOOLSETS="read,develop" limits which tools are offered (fewer tools = fewer tokens,
# smaller blast radius). Default: all. "read" is always on.
TOOLSETS: dict[str, set[str]] = {
    "read": {
        "connection_status", "lightroom_selftest", "list_collections", "list_photos",
        "get_photo_metadata", "get_photo_previews", "get_contact_sheet", "get_develop_settings",
        "list_presets", "backlog_report", "list_changes",
    },
    "edit": {
        "set_rating", "set_flag", "add_keywords", "set_color_label", "set_title_caption",
        "create_collection", "add_to_collection", "export_photos", "undo_last_change",
    },
    "develop": {
        "apply_preset", "apply_develop_settings", "create_preset", "sync_develop_settings",
        "create_virtual_copies",
    },
    "mhphotos": {
        "send_to_mhphotos_inbox", "mhphotos_galleries", "mhphotos_inbox", "mhphotos_analyze_inbox",
        "mhphotos_publish", "mhphotos_instagram_posts", "mhphotos_instagram_images",
        "mhphotos_schedule_instagram", "mhphotos_instagram_queue", "mhphotos_cancel_instagram",
        "mhphotos_inbox_images", "mhphotos_inbox_contact_sheet", "mhphotos_edit_preview",
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

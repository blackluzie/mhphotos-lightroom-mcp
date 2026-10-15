"""MCP server: read tools always on, edit/export tools behind a permission gate.

Adapter is chosen with MHPHOTOS_ADAPTER=mock (default) | bridge.
"""

import os
import re
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP, Image

from . import develop, permissions, publishing
from .adapters import (
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
async def list_collections(name_contains: str | None = None) -> dict[str, Any]:
    """List collections (albums) with id, name and photo count.

    name_contains: case-insensitive part of the name, e.g. "09 2026". Collection sets
    appear as "Set/Name".
    """
    result = await _run(_adapter.list_collections)
    if _is_error(result):
        return result
    needle = (name_contains or "").strip().lower()
    return {
        "collections": [c.summary() for c in result if needle in c.name.lower()]
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


@mcp.tool()
async def send_to_mhphotos_inbox(photo_ids: list[str], confirm: bool = False) -> dict[str, Any]:
    """Export photos and send them to the MHPhotos Foto-Inbox for review (not published).

    Items arrive with status "wartet" and are approved inside the MHPhotos app. Dry run
    unless confirm=true. Needs operator config: MHPHOTOS_INBOX_URL, MHPHOTOS_ADMIN_TOKEN
    and MHPHOTOS_EXPORT_DIR.
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
    if not confirm:
        return {
            "dry_run": True,
            "target": cfg.host,
            "files": [p.filename for p in photos],
            "hint": "Nothing was exported or sent. Call again with confirm=true.",
        }
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
    return {"applied": True, "target": cfg.host, "export_folder": str(batch_dir), "results": results}


# ---------------------------------------------------------------- editing advice
MAX_PREVIEWS = 12
MAX_SETTINGS_BATCH = 20


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
async def apply_preset(photo_ids: list[str], preset: str, confirm: bool = False) -> dict[str, Any]:
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
    )


@mcp.tool()
async def apply_develop_settings(
    photo_ids: list[str], settings: dict[str, Any], confirm: bool = False
) -> dict[str, Any]:
    """Apply develop settings (e.g. {"Exposure2012": 0.3, "Highlights2012": -40}) to photos.

    Only known parameters within Lightroom's ranges are accepted (error lists them). Dry run
    shows old and new values per photo. Needs operator opt-in and confirm=true; Lightroom
    records a history step that can be undone.
    """
    if not photo_ids or len(photo_ids) > MAX_SETTINGS_BATCH or len(set(photo_ids)) != len(photo_ids):
        return {"error": f"Provide 1-{MAX_SETTINGS_BATCH} distinct photo ids"}
    clean, err = develop.validate(settings)
    if err:
        return {"error": err}
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
            "photos": current,
            "hint": "Nothing was changed. Call again with confirm=true to apply.",
        }
    result = await _run(_adapter.apply_develop_settings, photo_ids, clean)
    return result if _is_error(result) else {"applied": True, "new": clean, **result}


@mcp.tool()
async def create_preset(name: str, settings: dict[str, Any], confirm: bool = False) -> dict[str, Any]:
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
    result = await _run(_adapter.create_preset, name.strip(), clean)
    return result if _is_error(result) else {"applied": True, **result}


# ---------------------------------------------------------------- MHPhotos app
MAX_INBOX_BATCH = 20
_OVERRIDE_KEYS = {"title": 120, "description": 2000, "alt": 220, "location": 120}


def _app_cfg() -> publishing.InboxConfig | dict[str, Any]:
    cfg = publishing.inbox_config()
    if cfg is None:
        return {"error": publishing.disabled_message(), "code": "disabled"}
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
    for entry in ready:
        fields = {k: entry[k] for k in ("title", "description", "alt", "location", "categories")}
        code, data = await anyio.to_thread.run_sync(publishing.approve, cfg, entry["id"], fields)
        if code == 200 and data.get("ok"):
            entry["status"] = "published"
            entry["homepage_photo_id"] = (data.get("photo") or {}).get("id")
        else:
            entry["status"] = "error"
            entry["problem"] = _app_error(code, data)["error"]
    return {"applied": True, "published": sum(1 for e in plan if e.get("status") == "published"), "items": plan}


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

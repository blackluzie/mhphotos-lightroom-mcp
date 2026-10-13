"""MCP tools for a future read-only Lightroom Classic integration."""

import asyncio
import hashlib
import json
import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

from mhphotos_lightroom_mcp.bridge import (
    BridgeConfig,
    read_collections,
    read_selection,
    read_status,
)
from mhphotos_lightroom_mcp.collection_schema import (
    COLLECTION_KINDS,
    validate_collections,
)

mcp = FastMCP("MHPhotos Lightroom MCP")


@mcp.tool()
def connection_status() -> dict:
    """Report local bridge readiness without claiming Lightroom catalog access."""
    token = os.environ.get("MHPHOTOS_BRIDGE_TOKEN", "")
    if not token:
        return {
            "connected": False,
            "mode": "prototype",
            "message": "Bridge not configured. No Lightroom catalog access.",
        }

    result = read_status(BridgeConfig(token=token))
    if result.get("reason") == "unauthorized":
        return {
            "connected": False,
            "mode": "prototype",
            "message": "Local bridge rejected the token. Check bridge configuration.",
        }
    if not result.get("bridge_running"):
        return {
            "connected": False,
            "mode": "prototype",
            "message": "Local bridge unavailable. No Lightroom catalog access.",
        }
    state = result.get("selection_state")
    messages = {
        "received": "Local bridge running; a submitted Lightroom selection is available. No live catalog connection.",
        "expired": "Local bridge running; the last selection expired. Send the selection from Lightroom again.",
        "none": "Local bridge running; no selection received yet. Send selected photos from Lightroom.",
    }
    status = {
        "connected": False,
        "mode": "prototype",
        "bridge_running": True,
        "selection_received": bool(result.get("selection_received")),
        "catalog_writes": False,
        "message": messages.get(
            state, "Local bridge running; Lightroom Classic is not connected."
        ),
    }
    if result.get("lightroom_worker_online") is True:
        status.update(connected=True, mode="read_only_requests",
                      message="Authenticated read-only request worker online. No catalog writes.")
    # Preserve compatibility with old bridges while exposing bounded diagnostics.
    for key in (
        "selection_state",
        "selection_count",
        "selection_age_seconds",
        "selection_expires_in_seconds",
        "protocol_version",
        "collection_state",
        "collection_count",
        "collection_age_seconds",
        "collection_expires_in_seconds",
        "collection_overview_truncated",
        "live_reads_enabled",
        "lightroom_worker_online",
    ):
        if key in result:
            status[key] = result[key]
    return status


@mcp.tool()
def selected_photo_metadata() -> dict:
    """Return only the last explicitly submitted selection, not live catalog data."""
    token = os.environ.get("MHPHOTOS_BRIDGE_TOKEN", "")
    if not token:
        return {"photos": [], "reason": "bridge_not_configured"}
    result = read_selection(BridgeConfig(token=token))
    if not isinstance(result.get("photos"), list):
        return {"photos": [], "reason": result.get("reason", "invalid_response")}
    snapshot = {"photos": result["photos"], "source": "local_bridge_snapshot"}
    if result.get("reason"):
        snapshot["reason"] = result["reason"]
    return snapshot


@mcp.tool()
def selected_photo_summary() -> dict:
    """Summarize only the last explicitly submitted photo selection.
    Group existing camera, lens, file-type, rating and pick-status display values;
    missing values are counted separately. Ratings and flags are read only.
    No filenames or new metadata are returned, and no catalog access occurs.
    Expired/unavailable snapshots return photo_count zero with a reason.
    """
    from mhphotos_lightroom_mcp.selection_summary import summarize_selection

    snapshot = selected_photo_metadata()
    if snapshot.get("reason"):
        return {"photo_count": 0, "reason": snapshot["reason"]}
    try:
        return summarize_selection(snapshot.get("photos", []))
    except ValueError:
        return {"photo_count": 0, "reason": "invalid_response"}


@mcp.tool()
def selected_photo_filter(
    file_name: str | None = None, camera_model: str | None = None,
    lens: str | None = None, file_type: str | None = None,
    rating: str | None = None, pick_status: str | None = None,
) -> dict:
    """Filter only the last submitted photo selection (maximum 50 photos).
    Filters are combined with AND. Name/camera/lens use case-insensitive literal
    substrings; file type is case-insensitive exact text. Rating and pick status
    match exact submitted values (e.g. "0", "5", "-1"). An empty rating/flag
    matches an explicitly empty value, not an absent field or zero. At least one
    filter is required. No catalog search, file access, or metadata writes occur.
    """
    from mhphotos_lightroom_mcp.selection_filter import (
        filter_selection,
        validate_filters,
    )

    filters = {key: value for key, value in {
        "file_name": file_name, "camera_model": camera_model, "lens": lens,
        "file_type": file_type, "rating": rating, "pick_status": pick_status,
    }.items() if value is not None}
    try:
        validate_filters(filters)
    except ValueError:
        return {"photos": [], "photo_count": 0, "reason": "invalid_filter_request"}
    snapshot = selected_photo_metadata()
    if snapshot.get("reason"):
        return {"photos": [], "photo_count": 0, "reason": snapshot["reason"]}
    try:
        return filter_selection(snapshot.get("photos", []), filters)
    except ValueError:
        return {"photos": [], "photo_count": 0, "reason": "invalid_response"}


@mcp.tool()
def collection_overview() -> dict:
    """Read the last user-submitted collection-name hierarchy, not a live catalog."""
    token = os.environ.get("MHPHOTOS_BRIDGE_TOKEN", "")
    if not token:
        return {"collections": [], "reason": "bridge_not_configured"}
    result = read_collections(BridgeConfig(token=token))
    if not isinstance(result.get("collections"), list):
        return {"collections": [], "reason": result.get("reason", "invalid_response")}
    if result.get("reason"):
        return {"collections": [], "reason": result["reason"]}
    if len(result["collections"]) > 100:
        return _collection_page(result, 0, 100, None)
    return {
        "collections": result["collections"],
        "truncated": result.get("truncated", False),
        "source": "local_bridge_snapshot",
    }


def _collection_page(
    result: dict, offset: int, limit: int, snapshot_id: str | None,
    *, identity: dict | None = None,
) -> dict:
    records = result["collections"]
    identifier = hashlib.sha256(
        json.dumps(
            identity if identity is not None else result,
            sort_keys=True, ensure_ascii=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if snapshot_id is not None and snapshot_id != identifier:
        return {"collections": [], "reason": "snapshot_changed"}
    if offset > len(records):
        return {"collections": [], "reason": "invalid_offset"}
    end = min(offset + limit, len(records))
    return {
        "collections": records[offset:end],
        "truncated": result.get("truncated", False),
        "source": "local_bridge_snapshot",
        "snapshot_id": identifier,
        "total_count": len(records),
        "offset": offset,
        "next_offset": end if end < len(records) else None,
    }


@mcp.tool()
def collection_overview_page(
    offset: int = 0, limit: int = 100, snapshot_id: str | None = None
) -> dict:
    """Read up to 100 submitted collection names. For later pages pass the
    snapshot_id and next_offset from the previous page. A new submission or
    expiry requires restarting at offset zero. Parent IDs may refer to earlier
    pages. truncated means Lightroom omitted entries, not that more pages exist.
    """
    if not _valid_collection_page(offset, limit, snapshot_id):
        return {"collections": [], "reason": "invalid_page_request"}
    token = os.environ.get("MHPHOTOS_BRIDGE_TOKEN", "")
    if not token:
        return {"collections": [], "reason": "bridge_not_configured"}
    result = read_collections(BridgeConfig(token=token))
    if not isinstance(result.get("collections"), list) or result.get("reason"):
        return {"collections": [], "reason": result.get("reason", "invalid_response")}
    return _collection_page(result, offset, limit, snapshot_id)


def _valid_collection_page(offset: int, limit: int, snapshot_id: str | None) -> bool:
    return not (
        type(offset) is not int or offset < 0
        or type(limit) is not int or not 1 <= limit <= 100
        or (snapshot_id is not None and (
            not isinstance(snapshot_id, str) or len(snapshot_id) != 64
            or any(c not in "0123456789abcdef" for c in snapshot_id)
        ))
        or (offset > 0 and snapshot_id is None)
    )


@mcp.tool()
def collection_search(
    query: str, kind: str | None = None, offset: int = 0, limit: int = 100,
    snapshot_id: str | None = None,
) -> dict:
    """Search names and ancestor names in the submitted collection snapshot.
    Case-insensitive literal substring; no regex or live catalog access.
    kind optionally selects collection, smart_collection or collection_set.
    Results include collection_path (ancestor names followed by the name).
    Use the same query/kind and returned snapshot_id/next_offset for later pages.
    An empty query is allowed only with a kind filter. Source truncation and
    five-minute expiry apply even when the search itself returns no matches.
    """
    if (
        not isinstance(query, str) or len(query) > 80
        or any(ord(c) < 32 or ord(c) == 127 for c in query)
        or (kind is not None and (not isinstance(kind, str) or kind not in COLLECTION_KINDS))
        or (not query.strip() and kind is None)
    ):
        return {"collections": [], "reason": "invalid_search_request"}
    if not _valid_collection_page(offset, limit, snapshot_id):
        return {"collections": [], "reason": "invalid_page_request"}
    token = os.environ.get("MHPHOTOS_BRIDGE_TOKEN", "")
    if not token:
        return {"collections": [], "reason": "bridge_not_configured"}
    overview = read_collections(BridgeConfig(token=token))
    if overview.get("reason"):
        return {"collections": [], "reason": overview["reason"]}
    try:
        records = validate_collections(overview)
    except ValueError:
        return {"collections": [], "reason": "invalid_response"}
    normalized = query.strip().casefold()
    paths, matches = {}, []
    for record in records:
        path = [*paths.get(record.get("parentId"), []), record["name"]]
        paths[record["id"]] = path
        if (kind is None or record["kind"] == kind) and normalized in " / ".join(path).casefold():
            matches.append({**record, "collection_path": path})
    result = _collection_page(
        {"collections": matches, "truncated": overview["truncated"]},
        offset, limit, snapshot_id,
        identity={"overview": overview, "query": normalized, "kind": kind},
    )
    if not result.get("reason"):
        result["query"] = query.strip()
        result["kind"] = kind
        result["snapshot_count"] = len(records)
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def lightroom_collections(query: str = "") -> dict:
    """Read current collection names/paths from the explicitly enabled Mac worker.
    Optional literal case-insensitive name/ancestor filter, maximum 100 matches.
    No manual send is required. Lightroom must be open and its read-only worker
    enabled. Available only through the isolated test profile, not public HTTP.
    """
    from mhphotos_lightroom_mcp.live_worker_io import post_live

    if not isinstance(query, str) or len(query) > 80 or any(ord(c)<32 or ord(c)==127 for c in query):
        return {"collections": [], "reason": "invalid_live_request"}
    try:
        result = await asyncio.to_thread(post_live, "read", {"operation": "collections"}, timeout=35)
    except (OSError, ValueError):
        return {"collections": [], "reason": "live_bridge_unavailable"}
    if result.get("error"):
        return {"collections": [], "reason": result["error"]}
    paths, matches = {}, []
    for record in result["collections"]:
        path = [*paths.get(record.get("parentId"), []), record["name"]]
        paths[record["id"]] = path
        if query.strip().casefold() in " / ".join(path).casefold():
            matches.append({**record, "collection_path": path})
    return {"collections": matches[:100], "match_count": len(matches),
            "truncated": result["truncated"], "more_matches": len(matches)>100,
            "session_id": result["session_id"], "source": "live_lightroom_read", "catalog_writes": False}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False), structured_output=False)
async def lightroom_collection_photos(
    collection_path: list[str], offset: int = 0, limit: int = 10,
    include_previews: bool = True, session_id: str | None = None,
) -> list[TextContent | ImageContent]:
    """Read current photos and JPEG previews from an exact collection name path.
    Maximum 10 photos with previews or 50 without. Pass returned session_id for
    further pages to reject a switched/reopened catalog. Each page is a fresh
    read; Lightroom's membership/order may change between calls. No image edits,
    rating writes or presets are executed. Requires the enabled private worker.
    """
    from mhphotos_lightroom_mcp.live_reads import validate_request
    from mhphotos_lightroom_mcp.live_worker_io import post_live

    request = {"operation": "collection_photos", "path": collection_path,
               "offset": offset, "limit": limit, "previews": include_previews,
               "session_id": session_id}
    try:
        validate_request(request)
    except (ValueError, UnicodeError):
        return [TextContent(type="text", text=json.dumps({"photos": [], "reason": "invalid_live_request"}))]
    try:
        result = await asyncio.to_thread(post_live, "read", request, timeout=35)
    except (OSError, ValueError):
        result = {"error": "live_bridge_unavailable"}
    if result.get("error"):
        return [TextContent(type="text", text=json.dumps({"photos": [], "reason": result["error"]}))]
    previews = result.pop("previews")
    result.update(source="live_lightroom_read", catalog_writes=False)
    result["preview_status"] = [{"index":p["index"], "status":p.get("error", "available")} for p in previews]
    content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=True))]
    for preview in previews:
        if "jpeg" in preview:
            content.append(TextContent(type="text", text="Preview for photo " + str(offset + preview["index"])))
            content.append(ImageContent(type="image", data=preview["jpeg"], mimeType="image/jpeg"))
    return content


def register_http_tools(http_server: FastMCP) -> None:
    """Publish the read-only tools with explicit ChatGPT permissions."""
    from mcp.types import ToolAnnotations

    from mhphotos_lightroom_mcp.oauth import SCOPE

    for tool in (
        connection_status, selected_photo_metadata, selected_photo_summary,
        selected_photo_filter, collection_overview,
        collection_overview_page, collection_search,
    ):
        http_server.add_tool(
            tool,
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
            meta={"securitySchemes": [{"type": "oauth2", "scopes": [SCOPE]}]},
        )


def main() -> None:
    """Run MCP server over stdio or Streamable HTTP."""
    transport = os.environ.get("MHPHOTOS_MCP_TRANSPORT", "stdio")

    if transport == "streamable-http":
        from mhphotos_lightroom_mcp.oauth import create_http_server

        http_server = create_http_server()
        register_http_tools(http_server)
        import uvicorn

        uvicorn.run(
            http_server.streamable_http_app(),
            host="127.0.0.1",
            port=8766,
            access_log=False,
            proxy_headers=False,
        )
    elif transport == "stdio":
        mcp.run(transport="stdio")
    else:
        raise ValueError("Unsupported MHPHOTOS_MCP_TRANSPORT")


if __name__ == "__main__":
    main()

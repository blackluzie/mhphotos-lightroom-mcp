"""MCP tools for a future read-only Lightroom Classic integration."""

import hashlib
import json
import os

from mcp.server.fastmcp import FastMCP

from mhphotos_lightroom_mcp.bridge import (
    BridgeConfig,
    read_collections,
    read_selection,
    read_status,
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


def _collection_page(result: dict, offset: int, limit: int, snapshot_id: str | None) -> dict:
    records = result["collections"]
    identifier = hashlib.sha256(
        json.dumps(result, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
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
    if (
        type(offset) is not int or offset < 0
        or type(limit) is not int or not 1 <= limit <= 100
        or (snapshot_id is not None and (
            not isinstance(snapshot_id, str) or len(snapshot_id) != 64
            or any(c not in "0123456789abcdef" for c in snapshot_id)
        ))
        or (offset > 0 and snapshot_id is None)
    ):
        return {"collections": [], "reason": "invalid_page_request"}
    token = os.environ.get("MHPHOTOS_BRIDGE_TOKEN", "")
    if not token:
        return {"collections": [], "reason": "bridge_not_configured"}
    result = read_collections(BridgeConfig(token=token))
    if not isinstance(result.get("collections"), list) or result.get("reason"):
        return {"collections": [], "reason": result.get("reason", "invalid_response")}
    return _collection_page(result, offset, limit, snapshot_id)



def register_http_tools(http_server: FastMCP) -> None:
    """Publish the read-only tools with explicit ChatGPT permissions."""
    from mcp.types import ToolAnnotations

    from mhphotos_lightroom_mcp.oauth import SCOPE

    for tool in (connection_status, selected_photo_metadata, collection_overview, collection_overview_page):
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

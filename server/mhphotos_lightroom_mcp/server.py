"""MCP tools for a future read-only Lightroom Classic integration."""

import os

from mcp.server.fastmcp import FastMCP

from mhphotos_lightroom_mcp.bridge import BridgeConfig, read_selection, read_status

mcp = FastMCP("MHPhotos Lightroom MCP")


@mcp.tool()
def connection_status() -> dict[str, str | bool]:
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
    return {
        "connected": False,
        "mode": "prototype",
        "message": "Local bridge running; Lightroom Classic is not connected.",
    }


@mcp.tool()
def selected_photo_metadata() -> dict:
    """Return only the last explicitly submitted selection, not live catalog data."""
    token = os.environ.get("MHPHOTOS_BRIDGE_TOKEN", "")
    if not token:
        return {"photos": [], "reason": "bridge_not_configured"}
    result = read_selection(BridgeConfig(token=token))
    if not isinstance(result.get("photos"), list):
        return {"photos": [], "reason": result.get("reason", "invalid_response")}
    return {"photos": result["photos"], "source": "local_bridge_snapshot"}


def main() -> None:
    """Run MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

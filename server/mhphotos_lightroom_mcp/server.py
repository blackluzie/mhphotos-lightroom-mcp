"""Safe MCP prototype: no connection to a real Lightroom catalog yet."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("MHPhotos Lightroom MCP")


@mcp.tool()
def connection_status() -> dict[str, str | bool]:
    """Report whether a Lightroom Classic bridge is connected."""
    return {
        "connected": False,
        "mode": "prototype",
        "message": "Lightroom Classic bridge not installed. No catalog access.",
    }


def main() -> None:
    """Run MCP server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

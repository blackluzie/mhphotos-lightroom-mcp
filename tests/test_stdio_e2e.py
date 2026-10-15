"""Start the real server over stdio and call tools through an MCP client."""

import json
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_tools_over_stdio():
    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "mhphotos_lightroom_mcp.server"]
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert tools == {
                "connection_status",
                "list_collections",
                "list_photos",
                "get_photo_metadata",
            }
            res = await session.call_tool("get_photo_metadata", {"photo_id": "p4"})
            meta = json.loads(res.content[0].text)
            assert meta["camera"] == "Nikon Z8"

    anyio.run(run)

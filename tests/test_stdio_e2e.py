"""Start the real server over stdio and call tools through an MCP client."""

import json
import os
import socket
import sys
import time
from types import SimpleNamespace

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .fake_plugin import FakePlugin

ALL_TOOLS = {
    "connection_status",
    "lightroom_selftest",
    "list_collections",
    "list_photos",
    "get_photo_metadata",
    "set_rating",
    "set_flag",
    "add_keywords",
    "export_photos",
    "send_to_mhphotos_inbox",
    "mhphotos_galleries",
    "mhphotos_inbox",
    "mhphotos_analyze_inbox",
    "mhphotos_publish",
    "mhphotos_instagram_posts",
    "get_photo_previews",
    "mhphotos_instagram_images",
    "get_develop_settings",
    "list_presets",
    "apply_preset",
    "apply_develop_settings",
    "create_preset",
}


def _params(**env):
    full = {**os.environ, **env}
    return StdioServerParameters(
        command=sys.executable, args=["-m", "mhphotos_lightroom_mcp.server"], env=full
    )


def _text(res):
    return json.loads(res.content[0].text)


def test_tools_over_stdio():
    async def run():
        async with (
            stdio_client(_params()) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            assert {t.name for t in (await session.list_tools()).tools} == ALL_TOOLS
            meta = _text(await session.call_tool("get_photo_metadata", {"photo_id": "p4"}))
            assert meta["camera"] == "Nikon Z8"
            # previews arrive as real image content next to their caption
            res = await session.call_tool("get_photo_previews", {"photo_ids": ["p4", "p5"], "size": 512})
            kinds = [c.type for c in res.content]
            assert kinds == ["text", "image", "text", "image"]
            assert res.content[1].mimeType == "image/jpeg"
            assert "Seiser Alm" in res.content[0].text
            # writes are off by default
            res = _text(await session.call_tool("set_rating", {"photo_ids": ["p4"], "rating": 1, "confirm": True}))
            assert res["code"] == "disabled"

    anyio.run(run)


def test_bridge_mode_end_to_end(tmp_path):
    """Real server process in bridge mode + a fake plugin that reads bridge.json like the Lua one."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg_dir = tmp_path / "cfg"

    async def run():
        params = _params(
            MHPHOTOS_ADAPTER="bridge",
            MHPHOTOS_BRIDGE_PORT=str(port),
            MHPHOTOS_CONFIG_DIR=str(cfg_dir),
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            assert _text(await session.call_tool("connection_status", {}))["connected"] is False

            cfg = json.loads((cfg_dir / "bridge.json").read_text())
            plugin = FakePlugin(SimpleNamespace(url=cfg["url"], token=cfg["token"])).start()
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if _text(await session.call_tool("connection_status", {}))["connected"]:
                        break
                    await anyio.sleep(0.2)
                else:
                    raise AssertionError("plugin never connected")
                colls = _text(await session.call_tool("list_collections", {}))["collections"]
                assert {c["name"] for c in colls} == {"Portraits", "Landschaft", "Leer"}
                meta = _text(await session.call_tool("get_photo_metadata", {"photo_id": "p1"}))
                assert meta["rating"] == 5
            finally:
                plugin.stop()

    anyio.run(run)

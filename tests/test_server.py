from mhphotos_lightroom_mcp.server import connection_status


def test_disconnected_by_default():
    result = connection_status()
    assert result["connected"] is False
    assert result["mode"] == "prototype"


def test_only_read_only_tools_are_published():
    import asyncio

    from mhphotos_lightroom_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == {"connection_status", "selected_photo_metadata", "selected_photo_summary", "selected_photo_filter", "collection_overview", "collection_overview_page", "collection_search"}

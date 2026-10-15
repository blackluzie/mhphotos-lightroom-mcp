import pytest
from mhphotos_lightroom_mcp import server

pytestmark = pytest.mark.anyio


async def test_connection_status_reports_mock():
    result = await server.connection_status()
    assert result["connected"] is True
    assert result["mode"] == "mock"
    assert "No Lightroom catalog access" in result["message"]
    assert result["edits_enabled"] is False
    assert result["export_enabled"] is False


async def test_list_collections():
    colls = (await server.list_collections())["collections"]
    assert {c["name"] for c in colls} == {"Portraits", "Landschaft", "Leer"}
    assert next(c for c in colls if c["id"] == "c1")["photo_count"] == 3


async def test_list_photos_whole_catalog_and_paging():
    page = await server.list_photos(limit=2, offset=0)
    assert page["total"] == 5
    assert [p["id"] for p in page["photos"]] == ["p1", "p2"]
    page2 = await server.list_photos(limit=2, offset=4)
    assert [p["id"] for p in page2["photos"]] == ["p5"]


async def test_list_photos_by_collection():
    page = await server.list_photos(collection_id="c2")
    assert [p["filename"] for p in page["photos"]] == ["DSC_0100.nef", "DSC_0101.nef"]
    assert (await server.list_photos(collection_id="c3"))["photos"] == []


async def test_list_photos_clamps_limit_and_offset():
    page = await server.list_photos(limit=10_000, offset=-5)
    assert page["limit"] == 100
    assert page["offset"] == 0
    assert (await server.list_photos(limit=0))["limit"] == 1


async def test_unknown_ids_return_error():
    assert "error" in await server.list_photos(collection_id="nope")
    assert "error" in await server.get_photo_metadata("nope")


async def test_get_photo_metadata():
    meta = await server.get_photo_metadata("p1")
    assert meta["rating"] == 5
    assert meta["flag"] == "picked"
    assert meta["keywords"] == ["portrait", "studio"]
    assert meta["camera"] == "Canon EOS R5"

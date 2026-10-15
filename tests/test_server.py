import pytest
from mhphotos_lightroom_mcp import server
from mhphotos_lightroom_mcp.adapters import MockAdapter


@pytest.fixture(autouse=True)
def _mock_adapter():
    server.set_adapter(MockAdapter())


def test_connection_status_reports_mock():
    result = server.connection_status()
    assert result["connected"] is True
    assert result["mode"] == "mock"
    assert "No Lightroom catalog access" in result["message"]


def test_list_collections():
    colls = server.list_collections()
    assert {c["name"] for c in colls} == {"Portraits", "Landschaft", "Leer"}
    assert next(c for c in colls if c["id"] == "c1")["photo_count"] == 3


def test_list_photos_whole_catalog_and_paging():
    page = server.list_photos(limit=2, offset=0)
    assert page["total"] == 5
    assert [p["id"] for p in page["photos"]] == ["p1", "p2"]
    page2 = server.list_photos(limit=2, offset=4)
    assert [p["id"] for p in page2["photos"]] == ["p5"]


def test_list_photos_by_collection():
    page = server.list_photos(collection_id="c2")
    assert [p["filename"] for p in page["photos"]] == ["DSC_0100.nef", "DSC_0101.nef"]
    assert server.list_photos(collection_id="c3")["photos"] == []


def test_list_photos_clamps_limit_and_offset():
    page = server.list_photos(limit=10_000, offset=-5)
    assert page["limit"] == 100
    assert page["offset"] == 0
    assert server.list_photos(limit=0)["limit"] == 1


def test_unknown_ids_return_error():
    assert "error" in server.list_photos(collection_id="nope")
    assert "error" in server.get_photo_metadata("nope")


def test_get_photo_metadata():
    meta = server.get_photo_metadata("p1")
    assert meta["rating"] == 5
    assert meta["flag"] == "picked"
    assert meta["keywords"] == ["portrait", "studio"]
    assert meta["camera"] == "Canon EOS R5"

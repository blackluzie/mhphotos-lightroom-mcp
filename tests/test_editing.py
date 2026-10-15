"""Previews, develop settings, presets: reads are free, changes need opt-in + confirm."""

import pytest
from mcp.server.fastmcp import Image
from mhphotos_lightroom_mcp import develop, server
from mhphotos_lightroom_mcp.adapters import MockAdapter

pytestmark = pytest.mark.anyio


@pytest.fixture
def writes(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")


async def test_collection_name_filter():
    res = await server.list_collections(name_contains="land")
    assert [c["name"] for c in res["collections"]] == ["Landschaft"]
    assert len((await server.list_collections())["collections"]) == 3


async def test_place_and_gps_filters():
    by_text = await server.list_photos(location="südtirol")
    assert [p["id"] for p in by_text["photos"]] == ["p4", "p5"]
    near = await server.list_photos(near_lat=46.7, near_lon=11.4, radius_km=40)
    assert [p["id"] for p in near["photos"]] == ["p4", "p5"]
    tight = await server.list_photos(near_lat=46.54, near_lon=11.62, radius_km=5)
    assert [p["id"] for p in tight["photos"]] == ["p4"]
    assert (await server.list_photos(near_lat=51.96, near_lon=7.63, radius_km=10))["photos"][0]["id"] == "p1"
    combined = await server.list_photos(location="Südtirol", min_rating=4)
    assert [p["id"] for p in combined["photos"]] == ["p4"]


async def test_gps_filter_validation():
    assert "error" in await server.list_photos(near_lat=46.0)
    assert "error" in await server.list_photos(near_lat=99, near_lon=11, radius_km=5)
    assert "error" in await server.list_photos(near_lat=46, near_lon=11, radius_km=0)


async def test_metadata_has_place_and_exposure():
    meta = await server.get_photo_metadata("p4")
    assert meta["location"].startswith("Seiser Alm")
    assert meta["gps"] == {"latitude": 46.54, "longitude": 11.62}
    assert meta["iso"] == 100 and meta["shutter_speed"] == "1/250"


async def test_previews_return_captioned_images():
    out = await server.get_photo_previews(["p4", "p1"], size=99999)
    assert [type(o) for o in out] == [dict, Image, dict, Image]
    assert out[0]["id"] == "p4" and out[0]["rating"] == 4
    assert out[1].data[:2] == b"\xff\xd8"


async def test_previews_validation():
    assert "error" in (await server.get_photo_previews([]))[0]
    assert "error" in (await server.get_photo_previews([f"p{i}" for i in range(13)]))[0]
    out = await server.get_photo_previews(["p1", "nope"])
    assert "error" in out[-1]


async def test_develop_settings_are_filtered():
    res = await server.get_develop_settings("p1")
    assert res["settings"] == {"ProcessVersion": "15.4", "Exposure2012": 0.0, "Contrast2012": 0}
    assert "error" in await server.get_develop_settings("nope")


async def test_list_presets():
    assert len((await server.list_presets())["presets"]) == 3
    res = await server.list_presets(name_contains="süd")
    assert res["presets"] == [{"folder": "User Presets", "name": "Südtirol Warm"}]


async def test_apply_preset_disabled_by_default():
    res = await server.apply_preset(["p4"], "Südtirol Warm", confirm=True)
    assert res["code"] == "disabled"


async def test_apply_preset_flow(writes):
    dry = await server.apply_preset(["p4", "p5"], "Südtirol Warm")
    assert dry["dry_run"] and dry["change"] == {"preset": "User Presets/Südtirol Warm"}
    assert "_preset" not in await _raw(server, "p4")
    done = await server.apply_preset(["p4", "p5"], "Südtirol Warm", confirm=True)
    assert done["applied"] and done["updated"] == 2
    assert (await _raw(server, "p4"))["_preset"] == "Südtirol Warm"
    assert "error" in await server.apply_preset(["p4"], "Gibt es nicht", confirm=True)
    assert "error" in await server.apply_preset(["p4", "nope"], "Vivid", confirm=True)


async def _raw(srv, pid):
    return srv._adapter._develop[pid]


async def test_apply_settings_validation(writes):
    for bad in (
        {},
        {"Unbekannt": 1},
        {"Exposure2012": 9},
        {"Exposure2012": "viel"},
        {"ConvertToGrayscale": 1},
        {"Exposure2012": True},
    ):
        assert "error" in await server.apply_develop_settings(["p4"], bad, confirm=True), bad
    assert "error" in await server.apply_develop_settings([], {"Exposure2012": 1})
    assert "error" in await server.apply_develop_settings([f"p{i}" for i in range(21)], {"Exposure2012": 1})


async def test_apply_settings_disabled_by_default():
    res = await server.apply_develop_settings(["p4"], {"Exposure2012": 0.3}, confirm=True)
    assert res["code"] == "disabled"
    assert (await _raw(server, "p4"))["Exposure2012"] == 0.0


async def test_apply_settings_dry_run_then_apply(writes):
    dry = await server.apply_develop_settings(["p4"], {"Exposure2012": 0.3, "Highlights2012": -40})
    assert dry["dry_run"] is True
    assert dry["photos"] == [{"id": "p4", "old": {"Exposure2012": 0.0, "Highlights2012": None}}]
    assert (await _raw(server, "p4"))["Exposure2012"] == 0.0
    done = await server.apply_develop_settings(["p4"], {"Exposure2012": 0.3, "Highlights2012": -40}, confirm=True)
    assert done["applied"] is True
    assert (await _raw(server, "p4"))["Highlights2012"] == -40


async def test_create_preset(writes):
    settings = {"Contrast2012": 15, "Vibrance": 20, "HueAdjustmentGreen": -10}
    dry = await server.create_preset("Mein Südtirol", settings)
    assert dry["dry_run"] and len((await server.list_presets())["presets"]) == 3
    done = await server.create_preset("Mein Südtirol", settings, confirm=True)
    assert done["applied"] and done["parameters"] == 3
    assert any(p["name"] == "Mein Südtirol" for p in (await server.list_presets())["presets"])
    assert "error" in await server.create_preset("Mein Südtirol", settings, confirm=True)  # duplicate
    assert "error" in await server.create_preset("", settings)
    assert "error" in await server.create_preset("a/b", settings)
    assert "error" in await server.create_preset("X", {"Bogus": 1})


async def test_create_preset_disabled_by_default():
    res = await server.create_preset("X", {"Contrast2012": 5}, confirm=True)
    assert res["code"] == "disabled"


def test_develop_validate_accepts_hsl_and_rejects_nan():
    clean, err = develop.validate({"SaturationAdjustmentBlue": -20, "ConvertToGrayscale": True})
    assert err is None and clean["SaturationAdjustmentBlue"] == -20
    assert develop.validate({"Exposure2012": float("nan")})[1] is not None
    assert isinstance(server._adapter, MockAdapter)

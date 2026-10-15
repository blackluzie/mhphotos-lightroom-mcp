"""Permission gate: nothing changes without operator opt-in AND confirm=true."""

import pytest
from mhphotos_lightroom_mcp import server

pytestmark = pytest.mark.anyio


async def test_edits_disabled_by_default():
    res = await server.set_rating(["p2"], 1, confirm=True)
    assert res["code"] == "disabled"
    assert (await server.get_photo_metadata("p2"))["rating"] == 3


async def test_dry_run_changes_nothing(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    res = await server.set_rating(["p2"], 1)
    assert res["dry_run"] is True
    assert res["photos"][0]["current"]["rating"] == 3
    assert (await server.get_photo_metadata("p2"))["rating"] == 3


async def test_confirmed_edits_apply(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    assert (await server.set_rating(["p2"], 1, confirm=True))["applied"] is True
    assert (await server.set_flag(["p2"], "picked", confirm=True))["applied"] is True
    assert (await server.add_keywords(["p2"], ["new", " portrait "], confirm=True))["applied"]
    meta = await server.get_photo_metadata("p2")
    assert meta["rating"] == 1
    assert meta["flag"] == "picked"
    assert meta["keywords"] == ["portrait", "new"]  # added, deduplicated, none removed


async def test_validation(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    assert "error" in await server.set_rating(["p1"], 6, confirm=True)
    assert "error" in await server.set_flag(["p1"], "bogus", confirm=True)
    assert "error" in await server.add_keywords(["p1"], ["", "  "], confirm=True)
    assert "error" in await server.set_rating([], 1, confirm=True)
    assert "error" in await server.set_rating(["p1", "p1"], 1, confirm=True)
    assert "error" in await server.set_rating([f"p{i}" for i in range(101)], 1, confirm=True)


async def test_unknown_id_changes_nothing(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    res = await server.set_rating(["p2", "nope"], 1, confirm=True)
    assert "error" in res
    assert (await server.get_photo_metadata("p2"))["rating"] == 3


async def test_export_needs_operator_dir(tmp_path, monkeypatch):
    assert (await server.export_photos(["p1"], confirm=True))["code"] == "disabled"
    monkeypatch.setenv("MHPHOTOS_EXPORT_DIR", "relative/dir")  # relative paths are rejected
    assert (await server.export_photos(["p1"], confirm=True))["code"] == "disabled"
    dest = tmp_path / "out"
    monkeypatch.setenv("MHPHOTOS_EXPORT_DIR", str(dest))
    dry = await server.export_photos(["p1"])
    assert dry["dry_run"] is True
    assert dry["files"] == ["IMG_0001.dng"]
    assert not dest.exists()
    done = await server.export_photos(["p1"], confirm=True)
    assert done["applied"] is True
    assert done["destination"] == str(dest)
    assert dest.is_dir()

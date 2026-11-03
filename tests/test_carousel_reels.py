"""Carousels from Lightroom photos and reel jobs through the MHPhotos app."""

import io
from datetime import UTC, datetime, timedelta

import pytest
from mhphotos_lightroom_mcp import audit, server
from mhphotos_lightroom_mcp.adapters import MockAdapter
from PIL import Image as PILImage

from .fake_mhphotos import FakeMHPhotos

pytestmark = pytest.mark.anyio


def future():
    return (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")


class Exporter(MockAdapter):
    def export_photos(self, photo_ids, dest_dir):
        colours = {"p1": "red", "p2": "lime", "p4": "blue"}
        for pid in photo_ids:
            name = self.get_photo(pid).filename.rsplit(".", 1)[0]
            PILImage.new("RGB", (1200, 900), colours.get(pid, "white")).save(f"{dest_dir}/{name}.jpg")
        return {"exported": photo_ids}


@pytest.fixture
def app(monkeypatch, tmp_path):
    f = FakeMHPhotos()
    monkeypatch.setenv("MHPHOTOS_INBOX_URL", f.url)
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "secret")
    monkeypatch.setenv("MHPHOTOS_EXPORT_DIR", str(tmp_path / "exp"))
    monkeypatch.setenv("MHPHOTOS_ALLOW_INSTAGRAM", "1")
    server.set_adapter(Exporter())
    yield f
    f.stop()


async def test_carousel_from_lightroom_in_order(app):
    dry = await server.mhphotos_schedule_instagram("Drei Farben", future(), photo_ids=["p2", "p1", "p4"])
    assert dry["dry_run"] and dry["carousel"] == ["IMG_0002.dng", "IMG_0001.dng", "DSC_0100.nef"]
    assert app.social_uploads == []
    done = await server.mhphotos_schedule_instagram("Drei Farben", future(), photo_ids=["p2", "p1", "p4"], confirm=True)
    assert done["applied"] is True
    up = app.social_uploads[0]
    assert up["type"] == b"post" and len(up["extra"]) == 2
    first = PILImage.open(io.BytesIO(up["file"])).convert("RGB").getpixel((5, 5))
    second = PILImage.open(io.BytesIO(up["extra"][0])).convert("RGB").getpixel((5, 5))
    assert first[1] > 200 and second[0] > 200  # lime first, then red: the given order
    assert audit.read_all()[-1]["photo_ids"] == ["p2", "p1", "p4"]


async def test_carousel_validation(app):
    for kwargs in ({"photo_ids": ["p1"]}, {"photo_ids": ["p1", "p1"]}, {"photo_ids": [f"x{i}" for i in range(11)]},
                   {"photo_ids": ["p1", "p2"], "post_type": "story"}, {"photo_ids": ["p1", "p2"], "photo_id": "p4"}):
        assert "error" in await server.mhphotos_schedule_instagram("x", future(), confirm=True, **kwargs), kwargs
    assert app.social_uploads == []


async def test_site_photos_search(app):
    res = await server.mhphotos_site_photos()
    assert [p["id"] for p in res["photos"]] == ["hp-2", "hp-1"]  # newest first
    assert [p["id"] for p in (await server.mhphotos_site_photos("münster"))["photos"]] == ["hp-1"]
    assert [p["id"] for p in (await server.mhphotos_site_photos("2026"))["photos"]] == ["hp-2"]


async def test_create_reel_is_a_gated_order_that_publishes_nothing(app, monkeypatch):
    ids = ["hp-1", "hp-2", "hp-3"]
    dry = await server.mhphotos_create_reel(ids, "Herbst", "Ein Herbst in Bildern")
    assert dry["dry_run"] and dry["published"] is False and dry["seconds"] == 8.4
    assert app.reel_jobs == []
    for bad in (["hp-1", "hp-2"], ["hp-1", "hp-1", "hp-2"]):
        assert "error" in await server.mhphotos_create_reel(bad, "t", "c", confirm=True)
    assert "error" in await server.mhphotos_create_reel(ids, " ", "c", confirm=True)
    done = await server.mhphotos_create_reel(ids, "Herbst", "Ein Herbst in Bildern", confirm=True)
    assert done["applied"] and done["job_id"] == "reel-0000" and done["status"] == "wartet"
    assert app.reel_jobs[0]["photoIds"] == ids
    jobs = await server.mhphotos_reel_jobs()
    assert jobs["jobs"][0]["status"] == "wartet" and jobs["jobs"][0]["caption"] == "Ein Herbst in Bildern"
    assert app.social_uploads == []
    monkeypatch.delenv("MHPHOTOS_ALLOW_INSTAGRAM")
    assert (await server.mhphotos_create_reel(ids, "t", "c", confirm=True))["code"] == "disabled"

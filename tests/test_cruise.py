"""Working from the MHPhotos inbox without Lightroom: look, edit a copy, schedule via the SocialHub."""

import io
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.fastmcp import Image
from mhphotos_lightroom_mcp import audit, imaging, publishing, server
from PIL import Image as PILImage

from .fake_mhphotos import FakeMHPhotos

pytestmark = pytest.mark.anyio


def future(minutes=0, days=1):
    return (datetime.now(UTC) + timedelta(days=days, minutes=minutes)).isoformat(timespec="seconds")


@pytest.fixture
def app(monkeypatch, tmp_path):
    f = FakeMHPhotos()
    monkeypatch.setenv("MHPHOTOS_INBOX_URL", f.url)
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "secret")
    monkeypatch.setenv("MHPHOTOS_EXPORT_DIR", str(tmp_path / "exp"))
    monkeypatch.setenv("MHPHOTOS_ALLOW_INSTAGRAM", "1")
    yield f
    f.stop()


# ---- seeing the inbox ----------------------------------------------------------------------
async def test_inbox_images_disabled_without_config():
    assert (await server.mhphotos_inbox_images(["lr-1"]))[0]["code"] == "disabled"
    assert (await server.mhphotos_inbox_contact_sheet(["lr-1"]))[0]["code"] == "disabled"
    assert (await server.mhphotos_edit_preview("lr-1", {"exposure": 0.3}))[0]["code"] == "disabled"


async def test_inbox_images(app):
    out = await server.mhphotos_inbox_images(["lr-1", "lr-2", "ghost"], size=300)
    assert out[0]["id"] == "lr-1" and out[0]["source"] == "lightroom-mobile" and out[0]["camera"] == "iPhone 15 Pro"
    assert isinstance(out[1], Image)
    img = PILImage.open(io.BytesIO(out[1].data))
    assert max(img.size) == 300 and img.format == "JPEG"  # shrunk from 1200x800
    assert out[2] == {"id": "lr-2", "filename": "IMG_0002.jpg", "status": "wartet", "rating": 5}
    assert isinstance(out[3], Image)
    assert out[4] == {"id": "ghost", "error": "not found in inbox"} and len(out) == 5


async def test_inbox_images_validation_and_missing_image(app):
    assert "error" in (await server.mhphotos_inbox_images([]))[0]
    assert "error" in (await server.mhphotos_inbox_images([f"x{i}" for i in range(13)]))[0]
    del app.images["lr-2"]
    out = await server.mhphotos_inbox_images(["lr-2"])
    assert out[0]["id"] == "lr-2" and "error" in out[0]


async def test_inbox_contact_sheet(app):
    legend, sheet = await server.mhphotos_inbox_contact_sheet(["lr-1", "lr-2", "lr-3", "nope"], columns=2, cell=200)
    assert [e["n"] for e in legend["legend"]] == [1, 2, 3, 4]
    assert legend["legend"][3]["error"] == "not available"
    assert PILImage.open(io.BytesIO(sheet.data)).size == (2 * 206 + 6, 2 * 206 + 6)
    assert "error" in (await server.mhphotos_inbox_contact_sheet([f"x{i}" for i in range(31)]))[0]


# ---- basic edits ---------------------------------------------------------------------------
def test_validate_adjust():
    assert imaging.validate_adjust(None) == ({}, None)
    assert imaging.validate_adjust({"exposure": 0.5, "crop": "4:5"})[1] is None
    for bad in ({}, {"exposure": 3}, {"exposure": "x"}, {"crop": "2:1"}, {"gamma": 1}, {"warmth": True}):
        assert imaging.validate_adjust(bad)[1] is not None, bad


def _flat(colour=(100, 100, 100), size=(400, 300)):
    out = io.BytesIO()
    PILImage.new("RGB", size, colour).save(out, "JPEG", quality=100)
    return out.getvalue()


def test_adjust_changes_pixels_and_crops():
    base = _flat()
    brighter = PILImage.open(io.BytesIO(imaging.adjust(base, {"exposure": 1.0}))).getpixel((10, 10))
    darker = PILImage.open(io.BytesIO(imaging.adjust(base, {"exposure": -1.0}))).getpixel((10, 10))
    assert brighter[0] > 180  # one stop up: about double (100 -> ~200)
    assert darker[0] < 70  # one stop down: about half (100 -> ~50)
    warm = PILImage.open(io.BytesIO(imaging.adjust(base, {"warmth": 50}))).getpixel((10, 10))
    assert warm[0] > warm[2]  # warmer: more red than blue
    cropped = PILImage.open(io.BytesIO(imaging.adjust(base, {"crop": "4:5"})))
    assert cropped.size[0] * 5 == cropped.size[1] * 4  # 240x300
    square = PILImage.open(io.BytesIO(imaging.adjust(base, {"crop": "1:1"})))
    assert square.size == (300, 300)
    wide = PILImage.open(io.BytesIO(imaging.adjust(_flat(size=(300, 400)), {"crop": "16:9"})))
    assert abs(wide.size[0] * 9 - wide.size[1] * 16) <= 16  # integer pixels: within one pixel of 16:9


def test_adjust_never_modifies_the_input_bytes():
    base = _flat()
    before = bytes(base)
    imaging.adjust(base, {"exposure": 1, "contrast": 20, "sharpen": 50})
    assert base == before


async def test_edit_preview_shows_before_and_after(app):
    info, image = await server.mhphotos_edit_preview("lr-1", {"exposure": 0.5, "crop": "1:1"}, size=300)
    assert info["adjust"] == {"exposure": 0.5, "crop": "1:1"}
    assert PILImage.open(io.BytesIO(image.data)).size == (2 * 300 + 3 * 8, 300 + 16)
    assert app.hub_uploads == []  # nothing saved or sent
    assert "error" in (await server.mhphotos_edit_preview("lr-1", {}))[0]
    assert "error" in (await server.mhphotos_edit_preview("lr-1", {"exposure": 9}))[0]
    assert (await server.mhphotos_edit_preview("ghost", {"exposure": 0.2}))[0]["id"] == "ghost"


# ---- Instagram via the SocialHub -----------------------------------------------------------
async def test_schedule_disabled_without_opt_in(app, monkeypatch):
    monkeypatch.delenv("MHPHOTOS_ALLOW_INSTAGRAM")
    res = await server.mhphotos_schedule_instagram("Hallo", future(), inbox_item_id="lr-1", confirm=True)
    assert res["code"] == "disabled" and app.hub_uploads == []


async def test_schedule_validation(app):
    ok = future()
    base = {"caption": "Hallo", "scheduled_at": ok, "inbox_item_id": "lr-1", "confirm": True}
    for override in (
        {"inbox_item_id": None},  # no source
        {"photo_id": "p4"},  # two sources
        {"caption": "  "},
        {"caption": "x" * 2201},
        {"post_type": "reel"},
        {"accounts": "instagram"},
        {"accounts": "mhphotos,mhphotos"},
        {"scheduled_at": "morgen"},
        {"scheduled_at": "2030-01-01T10:00:00"},
        {"scheduled_at": future(days=0, minutes=5)},
        {"scheduled_at": future(days=61)},
        {"adjust": {"exposure": 9}},
        {"inbox_item_id": "ghost"},
    ):
        assert "error" in await server.mhphotos_schedule_instagram(**{**base, **override}), override
    assert app.hub_uploads == []


async def test_schedule_from_inbox_dry_run_then_queue(app):
    when = future()
    dry = await server.mhphotos_schedule_instagram("Sonnenuntergang", when, inbox_item_id="lr-1")
    assert dry["dry_run"] and dry["accounts"] == ["mhphotos"] and dry["posted_automatically"] is True
    assert app.hub_uploads == []
    done = await server.mhphotos_schedule_instagram(
        "Sonnenuntergang", when, inbox_item_id="lr-1", adjust={"exposure": 0.3, "crop": "4:5"}, confirm=True
    )
    assert done["applied"] and done["queue_ids"] == ["q-0001"]
    up = app.hub_uploads[0]
    assert up["caption"] == b"Sonnenuntergang" and up["type"] == b"post" and up["accountId"] == b"mhphotos"
    assert up["imgHash"] == b"0123456789abcdef"  # from the SocialHub duplicate check
    sent = PILImage.open(io.BytesIO(up["file"]))
    assert sent.format == "JPEG" and sent.size[0] * 5 == sent.size[1] * 4  # cropped to 4:5, <= 1440 px
    assert datetime.fromisoformat(up["scheduledAt"].decode()).tzinfo is not None
    entry = audit.read_all()[-1]
    assert entry["tool"] == "mhphotos_schedule_instagram" and entry["change"]["inbox_item"] == "lr-1"


async def test_schedule_story_to_two_accounts(app):
    res = await server.mhphotos_schedule_instagram(
        "Moin", future(), inbox_item_id="lr-2", accounts="mhphotos,martin", post_type="story", confirm=True
    )
    assert len(res["queue_ids"]) == 2
    assert app.hub_uploads[0]["accountId"] == b"mhphotos,martin" and app.hub_uploads[0]["type"] == b"story"


async def test_schedule_refuses_duplicates_unless_allowed(app):
    app.duplicate = {"account_name": "MHPhotos", "scheduled_at": "2026-10-20T16:00:00.000Z", "status": "geplant", "distance": 2}
    res = await server.mhphotos_schedule_instagram("Nochmal", future(), inbox_item_id="lr-1", confirm=True)
    assert res["code"] == "duplicate" and res["duplicate"]["status"] == "geplant" and app.hub_uploads == []
    dry = await server.mhphotos_schedule_instagram("Nochmal", future(), inbox_item_id="lr-1", allow_duplicate=True)
    assert dry["dry_run"] and dry["duplicate"]["distance"] == 2
    ok = await server.mhphotos_schedule_instagram("Nochmal", future(), inbox_item_id="lr-1", allow_duplicate=True, confirm=True)
    assert ok["applied"] and len(app.hub_uploads) == 1


async def test_schedule_from_lightroom_photo(app):
    res = await server.mhphotos_schedule_instagram("Aasee", future(), photo_id="p4", confirm=True)
    assert res["applied"] and res["photo"] == "DSC_0100.nef"
    assert PILImage.open(io.BytesIO(app.hub_uploads[0]["file"])).format == "JPEG"
    assert audit.read_all()[-1]["photo_ids"] == ["p4"]


async def test_schedule_from_lightroom_needs_export_dir(app, monkeypatch):
    monkeypatch.delenv("MHPHOTOS_EXPORT_DIR")
    res = await server.mhphotos_schedule_instagram("Aasee", future(), photo_id="p4", confirm=True)
    assert res["code"] == "disabled"
    # an inbox item needs no export folder and no Lightroom
    ok = await server.mhphotos_schedule_instagram("Aasee", future(), inbox_item_id="lr-1", confirm=True)
    assert ok["applied"]


async def test_hub_error_is_reported(app):
    app.images["lr-1"] = b"not an image"
    res = await server.mhphotos_schedule_instagram("x", future(), inbox_item_id="lr-1", confirm=True)
    assert "error" in res and app.hub_uploads == []


async def test_queue_listing_and_cancel(app):
    await server.mhphotos_schedule_instagram("Hallo", future(), inbox_item_id="lr-1", confirm=True)
    items = (await server.mhphotos_instagram_queue())["items"]
    assert items[0]["id"] == "q-0001" and items[0]["status"] == "geplant"
    assert [i["id"] for i in (await server.mhphotos_instagram_queue(status="veroeffentlicht"))["items"]] == ["q-old"]
    dry = await server.mhphotos_cancel_instagram("q-0001")
    assert dry["dry_run"] and len(app.hub_queue) == 2
    assert (await server.mhphotos_cancel_instagram("q-0001", confirm=True))["applied"]
    assert [r["id"] for r in app.hub_queue] == ["q-old"]
    assert "already published" in (await server.mhphotos_cancel_instagram("q-old", confirm=True))["error"].lower()
    assert "not found" in (await server.mhphotos_cancel_instagram("q-nope", confirm=True))["error"]
    assert "queue_id" in (await server.mhphotos_cancel_instagram("../etc/passwd", confirm=True))["error"]


async def test_cancel_needs_opt_in(app, monkeypatch):
    monkeypatch.delenv("MHPHOTOS_ALLOW_INSTAGRAM")
    assert (await server.mhphotos_cancel_instagram("q-old", confirm=True))["code"] == "disabled"


def test_parse_accounts_and_ids():
    assert publishing.parse_accounts("mhphotos, martin") == ["mhphotos", "martin"]
    for bad in ("", "x", "mhphotos,mhphotos", "mhphotos;martin"):
        assert publishing.parse_accounts(bad) is None, bad
    assert publishing.parse_accounts("mhphotos,") == ["mhphotos"]  # a trailing comma is harmless
    assert publishing.valid_queue_id("q-1a2b3c4d") and not publishing.valid_queue_id("q-../x")

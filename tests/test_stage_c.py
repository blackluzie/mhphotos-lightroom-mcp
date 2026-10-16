"""Contact sheet, backlog report, Instagram scheduling."""

import io
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.fastmcp import Image
from mhphotos_lightroom_mcp import audit, imaging, server
from PIL import Image as PILImage

from .fake_mhphotos import FakeMHPhotos

pytestmark = pytest.mark.anyio


# ---- contact sheet ---------------------------------------------------------
async def test_contact_sheet_returns_one_image_with_legend():
    out = await server.get_contact_sheet(["p1", "p2", "p3", "p4", "p5", "p1-nope"], columns=3, cell=200)
    legend, image = out
    assert [e["n"] for e in legend["legend"]] == [1, 2, 3, 4, 5, 6]
    assert legend["legend"][3]["location"].startswith("Seiser Alm")
    assert legend["legend"][5]["error"] == "not available"
    assert isinstance(image, Image)
    img = PILImage.open(io.BytesIO(image.data))
    assert img.format == "JPEG" and img.size == (3 * 206 + 6, 2 * 206 + 6)  # 3 columns x 2 rows


async def test_contact_sheet_validation():
    assert "error" in (await server.get_contact_sheet([]))[0]
    assert "error" in (await server.get_contact_sheet(["p1", "p1"]))[0]
    assert "error" in (await server.get_contact_sheet([f"p{n}" for n in range(31)]))[0]


def test_contact_sheet_survives_broken_previews():
    good = io.BytesIO()
    PILImage.new("RGB", (300, 200), (10, 200, 10)).save(good, "JPEG")
    sheet = imaging.contact_sheet([good.getvalue(), b"not an image", None], columns=2, cell=160)
    img = PILImage.open(io.BytesIO(sheet))
    assert img.size == (2 * 166 + 6, 2 * 166 + 6)


def test_instagram_jpeg_shrinks_and_fits_the_limit():
    big = PILImage.effect_noise((4000, 3000), 90).convert("RGB")
    raw = io.BytesIO()
    big.save(raw, "JPEG", quality=95)
    out = imaging.instagram_jpeg(raw.getvalue(), limit_bytes=2_000_000)
    img = PILImage.open(io.BytesIO(out))
    assert max(img.size) == 1440 and len(out) <= 2_000_000


def test_instagram_jpeg_rejects_garbage():
    with pytest.raises(OSError):
        imaging.instagram_jpeg(b"definitely not an image")


# ---- backlog ---------------------------------------------------------------
async def test_backlog_report_uses_markers(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    adapter = server._adapter
    adapter.set_rating(["p2", "p3", "p4"], 5)  # more 5-star photos: p1(2026), p2, p3 (2026), p4 (2025)
    base = await server.backlog_report(2026)
    assert base["candidates"] == 3 and base["not_yet_handled"] == 3
    adapter.add_keywords(["p1"], ["MHPhotos-Inbox"])
    adapter.add_keywords(["p2"], ["Instagram"])
    rep = await server.backlog_report(2026)
    assert (rep["sent_to_mhphotos_inbox"], rep["marked_instagram"], rep["not_yet_handled"]) == (1, 1, 1)
    assert [p["id"] for p in rep["not_yet_handled_sample"]] == ["p3"]
    assert (await server.backlog_report(2025))["candidates"] == 1
    assert "error" in await server.backlog_report(26)
    assert "error" in await server.backlog_report(2026, min_rating=9)


# ---- Instagram scheduling ----------------------------------------------------
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


async def test_schedule_is_disabled_without_opt_in(app, monkeypatch):
    monkeypatch.delenv("MHPHOTOS_ALLOW_INSTAGRAM")
    res = await server.mhphotos_schedule_instagram("p4", "Hallo", future(), confirm=True)
    assert res["code"] == "disabled" and app.insta_uploads == []


async def test_schedule_validation(app):
    ok = future()
    for kwargs in (
        {"caption": "  "},
        {"caption": "x" * 2201},
        {"post_type": "reel"},
        {"scheduled_at": "morgen"},
        {"scheduled_at": "2030-01-01T10:00:00"},  # no time zone
        {"scheduled_at": future(days=0, minutes=5)},  # too soon to cancel
        {"scheduled_at": future(days=61)},
        {"photo_id": "ghost"},
    ):
        args = {"photo_id": "p4", "caption": "Hallo", "scheduled_at": ok, "confirm": True, **kwargs}
        assert "error" in await server.mhphotos_schedule_instagram(**args), kwargs
    assert app.insta_uploads == []


async def test_schedule_dry_run_then_queue(app):
    when = future()
    dry = await server.mhphotos_schedule_instagram("p4", "Sonnenaufgang", when)
    assert dry["dry_run"] and dry["posted_automatically"] is True and app.insta_uploads == []
    done = await server.mhphotos_schedule_instagram("p4", "Sonnenaufgang", when, confirm=True)
    assert done["applied"] and done["queue_id"] == "iq-1"
    up = app.insta_uploads[0]
    assert up["caption"] == b"Sonnenaufgang" and up["type"] == b"post" and up["source"] == b"mcp"
    assert PILImage.open(io.BytesIO(up["file"])).format == "JPEG"
    assert datetime.fromisoformat(up["scheduledAt"].decode()).tzinfo is not None
    assert audit.read_all()[-1]["tool"] == "mhphotos_schedule_instagram"


async def test_queue_listing_and_cancel(app):
    await server.mhphotos_schedule_instagram("p4", "Hallo", future(), confirm=True)
    items = (await server.mhphotos_instagram_queue())["items"]
    assert [i["id"] for i in items] == ["iq-old", "iq-1"] and items[1]["status"] == "geplant"
    dry = await server.mhphotos_cancel_instagram("iq-1")
    assert dry["dry_run"] and len(app.insta_queue) == 2
    assert (await server.mhphotos_cancel_instagram("iq-1", confirm=True))["applied"]
    assert [i["id"] for i in app.insta_queue] == ["iq-old"]
    assert "already published" in (await server.mhphotos_cancel_instagram("iq-old", confirm=True))["error"].lower()
    assert "not found" in (await server.mhphotos_cancel_instagram("nope", confirm=True))["error"]


async def test_cancel_needs_opt_in(app, monkeypatch):
    monkeypatch.delenv("MHPHOTOS_ALLOW_INSTAGRAM")
    assert (await server.mhphotos_cancel_instagram("iq-old", confirm=True))["code"] == "disabled"

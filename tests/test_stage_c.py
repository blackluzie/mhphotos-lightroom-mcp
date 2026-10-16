"""Contact sheet, backlog report, Instagram scheduling."""

import io

import pytest
from mcp.server.fastmcp import Image
from mhphotos_lightroom_mcp import imaging, server
from PIL import Image as PILImage

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

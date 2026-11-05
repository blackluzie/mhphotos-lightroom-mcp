"""Crops for Instagram formats: the geometry and the tool."""

import pytest
from mhphotos_lightroom_mcp import server, social_crop

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(("w", "h", "aspect", "fx", "fy", "expected"), [
    # portrait 3:4 -> 4:5 is a little wider: cut the top and bottom (93.75 % of the height)
    (3024, 4032, "4:5", 0.5, 0.5, {"CropLeft": 0, "CropRight": 1, "CropTop": 0.03125, "CropBottom": 0.96875}),
    (3024, 4032, "4:5", 0.5, 0.0, {"CropLeft": 0, "CropRight": 1, "CropTop": 0, "CropBottom": 0.9375}),
    (3024, 4032, "4:5", 0.5, 1.0, {"CropLeft": 0, "CropRight": 1, "CropTop": 0.0625, "CropBottom": 1}),
    # portrait 3:4 -> 9:16 is narrower: cut the sides (75 % of the width)
    (3024, 4032, "9:16", 0.5, 0.5, {"CropLeft": 0.125, "CropRight": 0.875, "CropTop": 0, "CropBottom": 1}),
    (3024, 4032, "9:16", 0.0, 0.5, {"CropLeft": 0, "CropRight": 0.75, "CropTop": 0, "CropBottom": 1}),
    (3024, 4032, "9:16", 1.0, 0.5, {"CropLeft": 0.25, "CropRight": 1, "CropTop": 0, "CropBottom": 1}),
    # landscape 4:3 -> 4:5 keeps 60 % of the width
    (4032, 3024, "4:5", 0.5, 0.5, {"CropLeft": 0.2, "CropRight": 0.8, "CropTop": 0, "CropBottom": 1}),
    (4032, 3024, "4:5", 0.1, 0.5, {"CropLeft": 0, "CropRight": 0.6, "CropTop": 0, "CropBottom": 1}),
    # already the right shape: the whole picture
    (4000, 5000, "4:5", 0.3, 0.3, {"CropLeft": 0, "CropRight": 1, "CropTop": 0, "CropBottom": 1}),
    (3000, 3000, "1:1", 0.5, 0.5, {"CropLeft": 0, "CropRight": 1, "CropTop": 0, "CropBottom": 1}),
])
def test_crop_settings(w, h, aspect, fx, fy, expected):
    got = social_crop.crop_settings(w, h, aspect, fx, fy)
    assert {k: got[k] for k in expected} == pytest.approx(expected)
    assert got["CropAngle"] == 0


def test_crop_settings_rejects_bad_input():
    with pytest.raises(ValueError, match="aspect"):
        social_crop.crop_settings(100, 100, "7:3")
    with pytest.raises(ValueError, match="positive"):
        social_crop.crop_settings(0, 100, "4:5")


async def test_crop_to_aspect_is_a_gated_write_on_virtual_copies(monkeypatch):
    assert (await server.crop_to_aspect(["p2"], "4:5", confirm=True))["code"] == "disabled"
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    assert "error" in await server.crop_to_aspect(["p2"], "7:3")
    assert "error" in await server.crop_to_aspect(["p2"], "4:5", focus_x=1.5)
    assert "error" in await server.crop_to_aspect([f"p{i}" for i in range(11)], "4:5")
    dry = await server.crop_to_aspect(["p2"], "9:16")
    assert dry["dry_run"] is True and not any("-vc" in k for k in server._adapter._photos)

    done = await server.crop_to_aspect(["p2"], "9:16", focus_x=0.5, confirm=True)
    assert done["applied"] is True and done["cropped"] == 1 and done["aspect"] == "9:16"
    [copy] = done["copies"]
    assert copy["source_id"] == "p2"
    assert server._adapter._photos[copy["id"]].extra["copy_name"] == "Story 9:16"
    crop = server._adapter._develop[copy["id"]]
    assert crop["CropLeft"] == pytest.approx(0.125) and crop["CropRight"] == pytest.approx(0.875)
    assert "CropLeft" not in server._adapter._develop["p2"]  # the original stays untouched


async def test_crop_to_aspect_without_copies_changes_the_photo_itself(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    done = await server.crop_to_aspect(["p3"], "4:5", focus_y=0.0, virtual_copies=False, confirm=True)
    assert done["copies"] is None and done["photo_ids"] == ["p3"]
    crop = server._adapter._develop["p3"]
    assert crop["CropTop"] == 0 and crop["CropBottom"] == pytest.approx(0.9375)


async def test_virtual_copies_can_be_named(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    assert "error" in await server.create_virtual_copies(["p1"], copy_name="x" * 61)
    done = await server.create_virtual_copies(["p1"], copy_name="Insta 4:5", confirm=True)
    assert server._adapter._photos[done["copies"][0]["id"]].extra["copy_name"] == "Insta 4:5"

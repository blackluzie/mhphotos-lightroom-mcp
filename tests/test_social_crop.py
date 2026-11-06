"""Crops for Instagram formats: the geometry and the tool."""

import dataclasses

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


async def test_crop_to_aspect_while_lightroom_still_makes_the_copies(monkeypatch):
    # 0.1.25: the plug-in's job is still running after the wait: nothing is cropped, the job id is handed on
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    calls = []
    monkeypatch.setattr(server._adapter, "create_virtual_copies", lambda ids, name=None: {
        "copies": [], "job_id": "vc4-1789", "state": "running"})
    monkeypatch.setattr(server._adapter, "apply_develop_settings", lambda *a: calls.append(a))
    out = await server.crop_to_aspect(["p2"], "9:16", confirm=True)
    assert out["code"] == "copies_pending" and out["job_id"] == "vc4-1789" and "virtual_copy_job" in out["hint"]
    assert calls == []


async def test_crop_to_aspect_on_copies_lightroom_is_still_finishing(monkeypatch):
    from mhphotos_lightroom_mcp.bridge import BridgeError

    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    monkeypatch.setattr(server._adapter, "create_virtual_copies", lambda ids, name=None: {
        "copies": [{"source_id": "p2", "id": "p2-copy"}], "job_id": "vc5-1789", "state": "done"})

    def busy(*_):
        raise BridgeError("photo_busy: Lightroom is still finishing the new virtual copy p2-copy")

    monkeypatch.setattr(server._adapter, "apply_develop_settings", busy)
    out = await server.crop_to_aspect(["p2"], "9:16", confirm=True)
    assert out["code"] == "photo_busy" and out["copies"] == [{"source_id": "p2", "id": "p2-copy"}]
    assert "virtual_copies=false" in out["hint"]


async def test_virtual_copy_job_tool():
    assert "error" in await server.virtual_copy_job("../etc")
    assert "error" in await server.virtual_copy_job("vc1-100")  # the mock has no Lightroom jobs


def test_straighten_keeps_corners_inside():
    import math
    w, h, angle = 6000, 4000, 3.0
    got = social_crop.crop_settings(w, h, "original", angle=angle)
    assert got["CropAngle"] == angle
    cw = (got["CropRight"] - got["CropLeft"]) * w
    ch = (got["CropBottom"] - got["CropTop"]) * h
    rad = math.radians(angle)
    assert cw * math.cos(rad) + ch * math.sin(rad) <= w + 1
    assert cw * math.sin(rad) + ch * math.cos(rad) <= h + 1
    assert cw / ch == pytest.approx(w / h, rel=1e-3)
    assert got["CropLeft"] + got["CropRight"] == pytest.approx(1)


def test_straighten_with_aspect_and_limits():
    got = social_crop.crop_settings(6000, 4000, "4:5", angle=-2.0)
    assert got["CropAngle"] == -2.0
    cw = (got["CropRight"] - got["CropLeft"]) * 6000
    ch = (got["CropBottom"] - got["CropTop"]) * 4000
    assert cw / ch == pytest.approx(0.8, rel=1e-3)
    with pytest.raises(ValueError):
        social_crop.crop_settings(6000, 4000, "4:5", angle=50)


@pytest.mark.parametrize(("size", "expected"), [
    ({"width": 3024, "height": 4032}, (3024, 4032)),
    # cropped size is stale (still the old 4:5 crop of the same picture): the uncropped size wins
    ({"width": 3024, "height": 3780, "full_width": 3024, "full_height": 4032}, (3024, 4032)),
    # uncropped size comes unrotated while the cropped one is portrait: turn it
    ({"width": 3024, "height": 4032, "full_width": 4032, "full_height": 3024}, (3024, 4032)),
    ({"width": 4032, "height": 3024, "full_width": 4032, "full_height": 3024}, (4032, 3024)),
])
def test_uncropped_size(size, expected):
    assert social_crop.uncropped_size(size) == expected


def test_to_raw_orientation_turns_the_crop_into_the_stored_frame():
    shown = {"CropLeft": 0.125, "CropRight": 0.875, "CropTop": 0.0, "CropBottom": 1.0, "CropAngle": 0.0}
    cw = social_crop.to_raw_orientation(shown, "BC")
    assert (cw["CropLeft"], cw["CropRight"], cw["CropTop"], cw["CropBottom"]) == pytest.approx((0.0, 1.0, 0.125, 0.875))
    ccw = social_crop.to_raw_orientation(shown, "DA")
    assert (ccw["CropLeft"], ccw["CropRight"], ccw["CropTop"], ccw["CropBottom"]) == pytest.approx((0.0, 1.0, 0.125, 0.875))
    corner = {"CropLeft": 0.0, "CropRight": 0.5, "CropTop": 0.0, "CropBottom": 1.0}  # left half as shown
    cw = social_crop.to_raw_orientation(corner, "BC")  # clockwise: shown left is stored bottom
    assert (cw["CropTop"], cw["CropBottom"]) == pytest.approx((0.5, 1.0))
    ccw = social_crop.to_raw_orientation(corner, "DA")  # counter-clockwise: shown left is stored top
    assert (ccw["CropTop"], ccw["CropBottom"]) == pytest.approx((0.0, 0.5))


async def test_crop_to_aspect_fixes_a_picture_stored_turned(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    server._adapter._photos["p2"] = dataclasses.replace(
        server._adapter._photos["p2"], extra={**server._adapter._photos["p2"].extra, "stored_turned": True})
    done = await server.crop_to_aspect(["p2"], "9:16", focus_x=0.5, virtual_copies=False, confirm=True)
    assert done["applied"] is True
    size = server._adapter.photo_dimensions(["p2"])["dimensions"]["p2"]
    assert size["width"] / size["height"] == pytest.approx(9 / 16, rel=0.01)

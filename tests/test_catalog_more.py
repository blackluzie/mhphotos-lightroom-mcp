"""0.1.27: duplicate folders, missing files per drive, GPS gaps, print candidates, photo calendar, weekly report."""

import pytest
from mhphotos_lightroom_mcp import catalog_audit as ca
from mhphotos_lightroom_mcp import catalog_insights as ci
from mhphotos_lightroom_mcp import server

from tests.test_catalog_insights import app, row, save_snapshot, writes  # noqa: F401 - fixtures

pytestmark = pytest.mark.anyio


def result(**findings):
    return {"finished_at": "2026-11-08 10:00:00", "findings": findings, "summary": {}}


# ---------------------------------------------------------------- duplicate folders
async def test_duplicate_folders_finds_a_card_imported_twice():
    photos = [row("a1", "2020-01-01T10:00:00", folder="/NAS/2020/orig"),
              row("a2", "2020-01-01T10:00:01", folder="/NAS/2020/orig"),
              row("b1", "2020-01-01T10:00:00", folder="/NAS/Import/copy"),
              row("b2", "2020-01-01T10:00:01", folder="/NAS/Import/copy"),
              row("c1", "2020-01-01T10:00:00", folder="/NAS/Mixed"),
              row("c2", "2020-05-01T10:00:00", folder="/NAS/Mixed")]
    dupes = [{"keep": {"id": "a1", "folder": "2020/orig"}, "remove": [{"id": "b1", "bytes": 100}, {"id": "c1", "bytes": 5}]},
             {"keep": {"id": "a2", "folder": "2020/orig"}, "remove": [{"id": "b2", "bytes": 100}]}]
    rows = ci.duplicate_folders(result(duplicates=dupes), photos)
    assert rows[0]["copy_folder"] == "/NAS/Import/copy" and rows[0]["original_folder"] == "/NAS/2020/orig"
    assert rows[0]["photos"] == 2 and rows[0]["whole_folder"] is True
    assert rows[1]["copy_folder"] == "/NAS/Mixed" and rows[1]["whole_folder"] is False

    save_snapshot(photos)
    ca.save_result(result(duplicates=dupes))
    res = await server.duplicate_folders()
    assert res["whole_folders"] == 1 and res["photos"] == 3
    assert "photo_ids" not in res["pairs"]["items"][0]


# ---------------------------------------------------------------- missing files
def test_missing_files_offline_drive_vs_really_missing():
    photos = [row("m1", "2020-01-01T10:00:00", folder="/Volumes/Archiv/2010"),
              row("m2", "2020-01-01T10:00:00", folder="/Volumes/Archiv/2010"),
              row("m3", "2020-01-01T10:00:00", folder="/Volumes/NAS/2020/x"),
              row("ok", "2020-01-01T10:00:00", folder="/Volumes/NAS/2020/x")]
    found = [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]
    res = ci.missing_by_drive({**result(missing_file=found), "check_files": True}, photos,
                              is_dir=lambda p: p == "/Volumes/NAS")
    archiv, nas = res["drives"]
    assert archiv["drive"] == "/Volumes/Archiv" and archiv["status"] == "offline" and archiv["photos"] == 2
    assert archiv["folders"][0]["whole_folder"] is True
    assert nas["status"] == "missing" and nas["folders"][0] == {"folder": "/Volumes/NAS/2020/x", "missing": 1,
                                                                 "photos_in_folder": 2, "whole_folder": False}
    assert ci.volume_of("/Users/martin/Bilder") == "/"


async def test_missing_files_tool_says_when_files_were_not_checked():
    save_snapshot([])
    ca.save_result(result(missing_file=[]))
    res = await server.missing_files_by_drive()
    assert res["missing_total"] == 0 and "check_files=true" in res["hint"]


# ---------------------------------------------------------------- GPS gaps
def test_gps_gaps_take_the_nearest_photo_in_time():
    photos = [row("phone", "2024-06-01T12:00:00", lat=46.6, lon=11.5, cam="iPhone"),
              row("phone2", "2024-06-01T12:40:00", lat=46.7, lon=11.6, cam="iPhone"),
              row("cam1", "2024-06-01T12:10:00", cam="X-T5"),
              row("cam2", "2024-06-01T12:35:00", cam="X-T5"),
              row("far", "2024-06-01T15:00:00", cam="X-T5"),
              row("video", "2024-06-01T12:01:00", fmt="VIDEO")]
    res = ci.gps_gaps(photos, window_minutes=30)
    got = {s["id"]: (s["lat"], s["minutes_apart"]) for s in res["suggestions"]}
    assert got == {"cam1": (46.6, 10.0), "cam2": (46.7, 5.0)}
    assert res["without_gps"] == 3 and res["by_month"] == {"2024-06": 2}
    assert ci.gps_gaps(photos, 30, captured_from="2024-06-02")["suggestions_total"] == 0
    assert ci.gps_groups(res["suggestions"]) == [{"lat": 46.6, "lon": 11.5, "photo_ids": ["cam1"]},
                                                 {"lat": 46.7, "lon": 11.6, "photo_ids": ["cam2"]}]


async def test_fill_gps_gaps_dry_run_then_writes(writes):  # noqa: F811
    save_snapshot([row("p9", "2024-06-01T12:00:00", lat=46.6, lon=11.5), row("p4", "2024-06-01T12:05:00"),
                   row("p5", "2024-06-01T12:06:00")])
    assert (await server.gps_gaps())["suggestions_total"] == 2
    assert (await server.fill_gps_gaps("2024-06-01", "2024-06-01", window_minutes=999))["error"]
    dry = await server.fill_gps_gaps("2024-06-01", "2024-06-01")
    assert dry["dry_run"] and dry["photos"] == 2 and dry["positions"] == 1
    done = await server.fill_gps_gaps("2024-06-01", "2024-06-01", confirm=True)
    assert done["applied"] and done["photos_written"] == 2
    assert server._adapter.get_photo("p4").extra["gps"] == {"latitude": 46.6, "longitude": 11.5}


# ---------------------------------------------------------------- print candidates
def test_print_candidates_by_size_after_cropping():
    photos = [row("big", "2020-01-01T10:00:00", r=5, w=8000, h=5000),
              row("cropped", "2020-01-01T10:00:00", r=5, w=8000, h=5000, cw=3000, ch=2000),
              row("small", "2020-01-01T10:00:00", r=4, w=1500, h=1000),
              row("three", "2020-01-01T10:00:00", r=3, w=8000, h=5000),
              row("rejected", "2020-01-01T10:00:00", r=5, pick=-1, w=8000, h=5000)]
    res = ci.print_candidates(photos, min_rating=4, min_size="45×30")
    sizes = {g["max_size"]: [i["id"] for i in g["items"]] for g in res["groups"]}
    assert sizes["120×80"] == ["big"] and sizes["45×30"] == ["cropped"]
    assert res["too_small"] == 1 and res["candidates"] == 2
    assert ci.max_print({"w": 3600, "h": 2400}) == "60×40"
    with pytest.raises(ValueError):
        ci.print_candidates(photos, min_size="A4")


async def test_print_tools_need_a_snapshot_with_sizes(writes):  # noqa: F811
    save_snapshot([row("old", "2020-01-01T10:00:00", r=5)])
    assert (await server.print_candidates())["code"] == "old_snapshot"
    save_snapshot([row("big", "2020-01-01T10:00:00", r=5, w=8000, h=5000)])
    res = await server.print_candidates()
    assert res["candidates"] == 1
    dry = await server.print_candidates_collections()
    assert dry["dry_run"] and dry["first_collections"] == ["Druck-Kandidaten › bis 120×80 cm › 5★ (1)"]


# ---------------------------------------------------------------- calendar
def test_calendar_picks_per_month_with_variety():
    photos = []
    for day in range(1, 6):  # five great January photos of the same year, two on the same day
        photos.append(row(f"j{day}", f"2020-01-0{day}T10:00:00", r=5, w=6000, h=4000))
    photos += [row("jsame", "2020-01-01T11:00:00", r=5, w=6000, h=4000),
               row("j2019", "2019-01-10T10:00:00", r=4, w=6000, h=4000),
               row("portrait", "2021-01-10T10:00:00", r=5, w=4000, h=6000),
               row("jul", "2018-07-10T10:00:00", r=5, w=6000, h=4000),
               row("tiny", "2018-07-11T10:00:00", r=5, w=1200, h=800)]
    res = ci.calendar_candidates(photos, per_month=3)
    jan = res["months"][0]
    ids = [s["id"] for s in jan["suggestions"]]
    assert len(ids) == 3 and "j2019" in ids and "portrait" not in ids and "jsame" not in ids
    assert [s["id"] for s in res["months"][6]["suggestions"]] == ["jul"]
    assert "Februar" in res["months_short"]
    assert [s["id"] for s in ci.calendar_candidates(photos, 3, orientation="portrait")["months"][0]["suggestions"]] == \
        ["portrait"]


async def test_calendar_collections_from_suggestions_or_own_pick(writes):  # noqa: F811
    save_snapshot([row("j1", "2020-01-01T10:00:00", r=5, w=6000, h=4000),
                   row("d1", "2020-12-01T10:00:00", r=5, w=6000, h=4000)])
    res = await server.calendar_candidates()
    assert res["calendar"] == 2027 and len(res["months"]) == 12
    dry = await server.calendar_collections()
    assert dry["dry_run"] and dry["root"] == "Kalender 2027" and dry["collections"] == 2
    assert dry["first_collections"][0] == "Kalender 2027 › Vorschläge › 01 Januar (1)"
    own = await server.calendar_collections(photo_ids_by_month={"3": ["p1", "p2"]}, confirm=True)
    assert own["applied"] and own["photos_added"] == 2
    assert server._adapter.review_collections[-1]["name"] == "03 März"
    assert (await server.calendar_collections(photo_ids_by_month={"13": ["p1"]}))["error"]


# ---------------------------------------------------------------- weekly report
async def test_weekly_report_collects_every_part(app):  # noqa: F811
    save_snapshot([row("u1", "2020-01-01T10:00:00"), row("u2", "2020-02-01T10:00:00", r=4)])
    res = await server.weekly_report(days=7)
    assert set(res) >= {"from", "to", "new_photos", "backlog", "online", "spots"}
    assert res["backlog"]["unreviewed_total"] == 1
    assert "error" not in res["online"], res["online"]
    assert (await server.weekly_report(days=0))["error"]
    assert "error" not in res["new_photos"] and "error" not in res["spots"]

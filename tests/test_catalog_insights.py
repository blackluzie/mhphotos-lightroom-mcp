"""Catalog insights (0.1.26): snapshot, year overview, folder moves, archive candidates, pre-sorting, keywords,
online marks, gear statistics, spots and trip drafts."""

import asyncio

import anyio
import pytest
from mhphotos_lightroom_mcp import catalog_audit as ca
from mhphotos_lightroom_mcp import catalog_insights as ci
from mhphotos_lightroom_mcp import (
    http_service,
    keyword_check,
    online_marks,
    presort,
    publishing,
    server,
)

from tests.fake_mhphotos import FakeMHPhotos

pytestmark = pytest.mark.anyio


@pytest.fixture
def app(monkeypatch):
    f = FakeMHPhotos()
    monkeypatch.setenv("MHPHOTOS_INBOX_URL", f.url)
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "secret")
    yield f
    f.stop()


@pytest.fixture
def writes(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")


def row(pid, t, folder="/NAS/2020-2029/2020/2020-01/2020-01-01", **kw):
    return {"id": pid, "t": t, "folder": folder, "file": f"{pid}.dng", "size": 1024 ** 3, "fmt": "DNG", **kw}


def save_snapshot(photos, year=None):
    ca.save_snapshot([[p.get(k) for k in ca.SNAPSHOT_FIELDS] for p in photos], {"finished_at": "2026-11-07 10:00:00",
                                                                                 "year": year})


async def _finish_audit():
    for _ in range(300):
        st = await server.catalog_audit_status()
        if st["status"] != "running":
            return st
        await anyio.sleep(0.01)
    raise AssertionError("audit did not finish")


# ---------------------------------------------------------------- snapshot, overview, folder moves
async def test_audit_writes_snapshot_history_and_overview():
    await server.catalog_audit_start()
    assert (await _finish_audit())["status"] == "done"
    snap = ca.load_snapshot()
    assert {p["id"] for p in snap["photos"]} == {"p1", "p2", "p3", "p4", "p5"}
    p3 = next(p for p in snap["photos"] if p["id"] == "p3")
    assert p3["folder"].endswith("2026/2026-05/2026-05-01") and p3["file"] == "IMG_0003.dng"
    assert set(ca.load_history()) == {"all"}

    ov = await server.catalog_audit_overview()
    y2026 = next(r for r in ov["years"] if r["year"] == "2026")
    assert y2026["photos"] == 3 and y2026["wrong_folder"] == 1 and y2026["open_points"] >= 1
    assert ov["history"]["all"] == ov["checked_at"]

    moves = await server.catalog_folder_moves()
    [m] = moves["items"]
    assert m["folder"].endswith("2026-05-01") and m["photos_to_move"] == 1 and m["photos_in_folder"] == 3
    assert m["whole_folder"] is False and m["targets"] == {"2020-2029/2026/2026-05/2026-05-02": 1}

    await server.catalog_audit_start(year=2025)
    await _finish_audit()
    assert set(ca.load_history()) == {"all", "2025"}
    assert len(ca.load_snapshot()["photos"]) == 5, "a year run keeps the snapshot of the whole catalog"


def test_folder_moves_whole_folder():
    result = {"findings": {"wrong_folder": [
        {"id": "a", "expected": "2021-07-14", "expected_path": "2020-2029/2021/2021-07/2021-07-14", "level": "day"},
        {"id": "b", "expected": "2021-07-14", "expected_path": "2020-2029/2021/2021-07/2021-07-14", "level": "day"},
    ]}}
    photos = [{"id": "a", "folder": "/x/2021-07-13"}, {"id": "b", "folder": "/x/2021-07-13"}]
    [m] = ci.folder_moves(result, photos)
    assert m["whole_folder"] is True and m["photos_to_move"] == 2


async def test_insight_tools_without_snapshot_explain_what_to_do():
    assert (await server.archive_candidates())["code"] == "no_snapshot"
    assert (await server.gear_usage())["code"] == "no_snapshot"
    assert "error" in await server.catalog_audit_overview()


# ---------------------------------------------------------------- archive candidates
async def test_archive_candidates_and_collections(writes):
    save_snapshot([
        row("old1", "2015-03-01T10:00:00", folder="/NAS/2015/a"),
        row("old2", "2015-03-01T10:01:00", folder="/NAS/2015/a"),
        row("old3", "2015-03-02T10:00:00", folder="/NAS/2015/b", r=3),            # rated: keep
        row("old4", "2015-03-02T10:00:00", folder="/NAS/2015/b"),
        row("new", "2025-01-01T10:00:00", folder="/NAS/2025/a"),                  # too young
        row("rej", "2025-01-01T10:00:00", folder="/NAS/2025/a", pick=-1),
        row("kw", "2014-01-01T10:00:00", folder="/NAS/2014", kw=2),               # has keywords
    ])
    res = await server.archive_candidates(min_age_years=5)
    assert res["candidates"] == 3 and res["gb"] == 3.0 and res["whole_folders"] == 1
    assert res["by_year"] == {"2015": {"photos": 3, "gb": 3.0}}
    assert res["rejected_any_age"] == {"photos": 1, "gb": 1.0}
    first = res["folders"]["items"][0]
    assert first == {"folder": "/NAS/2015/a", "candidates": 2, "photos_in_folder": 2, "gb": 2.0, "whole_folder": True,
                     "year": "2015"}

    dry = await server.archive_candidates_collections(include_rejected=True)
    assert dry["dry_run"] and dry["photos"] == 4 and dry["collections"] == 2
    done = await server.archive_candidates_collections(include_rejected=True, confirm=True)
    assert done["applied"] and done["photos_added"] == 0  # snapshot ids are not in the mock catalog
    made = server._adapter.review_collections
    assert {(c["root"], c["set"], c["name"]) for c in made} == {("Archiv-Kandidaten", "2015", "unberührt"),
                                                                ("Archiv-Kandidaten", "Abgelehnt", "alle Jahre")}


# ---------------------------------------------------------------- pre-sorting
def test_series_and_batches():
    recs = [{"id": f"a{i}", "t": f"2024-05-01T10:00:0{i}", "cam": "R5"} for i in range(4)]   # one burst
    recs += [{"id": "b", "t": "2024-05-01T10:05:00", "cam": "R5"},
             {"id": "c", "t": "2024-05-01T10:05:02", "cam": "iPhone"}]                          # other camera
    groups = presort.series(recs, 5)
    assert [[r["id"] for r in g] for g in groups] == [["a0", "a1", "a2", "a3"], ["b"], ["c"]]
    big = [{"id": f"x{i}", "t": f"2024-05-01T11:{i // 60:02d}:{i % 60:02d}", "cam": "R5"} for i in range(40)]
    work = presort.batches(presort.series(recs + big, 5), size=30)
    assert all(len(b["photo_ids"]) <= 30 for b in work)
    assert work[0]["series"][0] == ["a0", "a1", "a2", "a3"] and work[0]["batch"] == 1
    assert presort.month_range("2024-02") == ("2024-02-01", "2024-02-29") and presort.month_range("2024-13") is None


async def test_presort_month_apply_and_status(writes):
    plan = await server.presort_month("2025-06")
    assert plan["unreviewed"] == 1 and plan["batches"][0]["photo_ids"] == ["p5"]
    assert "error" in await server.presort_month("2025-6")

    assert "error" in await server.presort_apply("2025-06", top=["p5"], reject=["p5"])
    dry = await server.presort_apply("2025-06", reject=["p5"], batch=1)
    assert dry["dry_run"] and dry["first_collections"] == ["Vorsortieren › 2025-06 › Aussortieren (Vorschlag) (1)"]
    done = await server.presort_apply("2025-06", reject=["p5"], batch=1, finished=True, confirm=True)
    assert done["applied"] and done["photos_added"] == 1 and done["progress"]["done"] is True
    assert server._adapter.get_photo("p5").rating == 0, "stars are never changed"

    save_snapshot([row("u1", "2025-06-02T10:00:00"), row("u2", "2024-01-01T10:00:00"),
                   row("u3", "2023-01-01T10:00:00", r=2)])
    st = await server.presort_status()
    assert st["months_done"] == ["2025-06"] and st["unreviewed_total"] == 2
    assert [n["month"] for n in st["next"]] == ["2024-01"], "done months and rated photos are skipped"


# ---------------------------------------------------------------- keywords
def test_keyword_analysis():
    kws = [
        {"id": "1", "name": "Orte", "path": "Orte", "depth": 0, "children": 2, "photos": 0},
        {"id": "2", "name": "Barcelona", "path": "Orte > Barcelona", "depth": 1, "children": 0, "photos": 10},
        {"id": "3", "name": "barcelona", "path": "barcelona", "depth": 0, "children": 0, "photos": 4},
        {"id": "4", "name": "Valencia", "path": "Orte > Valencia", "depth": 1, "children": 0, "photos": 2},
        {"id": "5", "name": "Valencia", "path": "Venezuela > Valencia", "depth": 1, "children": 0, "photos": 1},
        {"id": "6", "name": "Berg", "path": "Berg", "depth": 0, "children": 0, "photos": 3},
        {"id": "7", "name": "Berge", "path": "Berge", "depth": 0, "children": 0, "photos": 9},
        {"id": "8", "name": "Leer", "path": "Leer", "depth": 0, "children": 0, "photos": 0},
    ]
    res = keyword_check.analyse(kws)
    f = res["findings"]
    [dup] = f["duplicates"]
    assert dup["keep"]["id"] == "2" and [k["id"] for k in dup["merge"]] == ["3"]
    assert f["same_name_elsewhere"][0]["name"] == "valencia"
    [var] = f["variants"]
    assert var["keep"]["id"] == "7" and var["merge"][0]["id"] == "6"
    assert [k["id"] for k in f["unused"]] == ["8"]
    assert res["counts"]["photos_on_duplicates"] == 4


async def test_keyword_check_and_merge(writes):
    started = await server.keyword_check_start()
    assert started["started"] is True
    for _ in range(200):
        st = await server.keyword_check_status()
        if st["status"] != "running":
            break
        await anyio.sleep(0.01)
    assert st["status"] == "done" and st["last_result"]["counts"]["keywords"] == 5
    dups = await server.keyword_check_results("duplicates")
    [g] = dups["items"]
    assert g["keep"]["id"] == "k2" and g["merge"][0]["id"] == "k3"
    assert (await server.keyword_check_results("unused"))["items"][0]["id"] == "k5"

    dry = await server.merge_keywords(["k3"], "k2")
    assert dry["dry_run"] and dry["into"] == "Orte > Südtirol" and dry["photos_affected"] == 1
    done = await server.merge_keywords(["k3"], "k2", confirm=True)
    assert done["applied"] and done["photos_moved"] == 1 and done["remaining"] == 0
    assert "error" in await server.merge_keywords(["k2"], "k2")


# ---------------------------------------------------------------- online marks
def test_online_matching_rules():
    items = [
        {"id": "i1", "status": "uebernommen", "originalFileName": "IMG_1-2.jpg", "captureDate": "2026-05-01T10:15:00.000Z"},
        {"id": "i2", "status": "wartet", "instagramId": "ig", "lightroomId": "p9"},
        {"id": "i3", "status": "uebernommen", "originalFileName": "Export_7.jpg", "captureDate": "2026-05-01T12:00:00.000Z",
         "camera": "R5"},
        {"id": "i4", "status": "wartet"},
        {"id": "i5", "status": "uebernommen", "originalFileName": "nope.jpg", "captureDate": "2026-05-03T00:00:00Z"},
    ]
    records = [{"id": "p1", "file": "IMG_1.DNG", "t": "2026-05-01T10:15:00"},
               {"id": "p9", "file": "IMG_9.DNG", "t": "2026-04-01T10:00:00"},
               {"id": "p3", "file": "IMG_3.DNG", "t": "2026-05-01T12:00:00", "cam": "R5"}]
    res = online_marks.match(items, records)
    got = {m["item_id"]: (m["photo_id"], m["how"], m["keywords"]) for m in res["matches"]}
    assert got == {"i1": ("p1", "capture time + file name", ["MHPhotos-Online"]),
                   "i2": ("p9", "lightroom id", ["Instagram"]),
                   "i3": ("p3", "capture time + camera", ["MHPhotos-Online"])}
    assert [u["item_id"] for u in res["unmatched"]] == ["i5"]
    assert online_marks.days_needed(items[:1]) == [("2026-04-30", "2026-05-02")]


async def test_mark_online_in_lightroom(app, writes):
    app.items["lr-3"]["captureDate"] = "2026-05-02T08:00:00.000Z"  # IMG_0003.jpg = p3 (IMG_0003.dng)
    app.items["lr-5"] = {"id": "lr-5", "originalFileName": "x.jpg", "status": "wartet", "instagramId": "ig9",
                         "lightroomId": "p4", "captureDate": "2025-06-12T05:42:10.000Z"}
    dry = await server.mark_online_in_lightroom()
    assert dry["dry_run"] and dry["matched"] == 2 and dry["photos_per_keyword"] == {"MHPhotos-Online": 1, "Instagram": 1}
    done = await server.mark_online_in_lightroom(confirm=True)
    assert done["applied"] and done["photos_tagged"] == 2
    assert "MHPhotos-Online" in server._adapter.get_photo("p3").keywords
    assert "Instagram" in server._adapter.get_photo("p4").keywords
    assert done["smart_collections_created"] == ["MHPhotos/Online auf mhphotos.de", "MHPhotos/Auf Instagram"]
    assert done["source"] == "lightroom", "without a catalog snapshot Lightroom is asked directly"


async def test_mark_online_uses_catalog_snapshot(app, writes, monkeypatch):
    app.items["lr-3"]["captureDate"] = "2026-05-02T08:00:00.000Z"
    app.items["lr-5"] = {"id": "lr-5", "originalFileName": "x.jpg", "status": "wartet", "instagramId": "ig9",
                         "lightroomId": "p4", "captureDate": "2025-06-12T05:42:10.000Z"}
    save_snapshot([row("p3", "2026-05-02T08:00:00", file="IMG_0003.dng")])  # p4 imported after the check

    async def no_lightroom(*_a, **_k):
        raise AssertionError("the dry run must not ask Lightroom when the snapshot has the photos")

    with monkeypatch.context() as m:
        m.setattr(server, "_range", no_lightroom)
        dry = await server.mark_online_in_lightroom()
    assert dry["source"] == "catalog snapshot" and dry["checked_at"] == "2026-11-07 10:00:00"
    assert dry["matched"] == 1 and dry["not_matched"] == 1 and "live_check=true" in dry["hint"]

    dry = await server.mark_online_in_lightroom(live_check=True)
    assert dry["matched"] == 2 and dry["not_matched"] == 0 and dry["live_checked"] == 1

    save_snapshot([row("p3", "2026-05-02T08:00:00", file="IMG_0003.dng")], year=2026)
    assert (await server.mark_online_in_lightroom())["source"] == "lightroom", "a one-year snapshot is not enough"


def test_upload_sends_lightroom_id(app, tmp_path):
    jpg = tmp_path / "IMG_1.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xd9")
    publishing.upload(publishing.inbox_config(), jpg, {"id": "uuid-1", "rating": 5})
    body = next(b for m, p, b in app.calls if p == "/api/admin/lightroom-import")
    assert body["lightroomId"] == b"uuid-1"


# ---------------------------------------------------------------- gear
GEAR_PHOTOS = [
    row("g1", "2024-05-01T10:00:00", cam="Canon EOS R5", lens="RF24-70mm F2.8 L IS USM", fl=24, r=5),
    row("g2", "2025-05-01T10:00:00", cam="Canon EOS R5", lens="RF24-70mm F2.8 L IS USM", fl=70, r=4),
    row("g3", "2025-06-01T10:00:00", cam="iPhone 15 Pro", lens="iPhone 15 Pro back camera 6.86mm f/1.78", fl=7),
]


async def test_gear_usage_and_sync(app):
    save_snapshot(GEAR_PHOTOS)
    res = await server.gear_usage()
    r5 = res["cameras"][0]
    assert r5["name"] == "Canon EOS R5" and r5["photos"] == 2 and r5["five_star"] == 1 and r5["rated_4_plus"] == 2
    assert r5["first_used"] == "2024-05-01" and r5["last_used"] == "2025-05-01"
    lens = res["lenses"][0]
    assert lens["top_focal_lengths"] == {"24 mm": 1, "70 mm": 1}
    gear = res["mhphotos_gear"]
    assert {m["gear_id"] for m in gear["matched"]} == {"g-r5", "g-2470"}
    assert [u["name"] for u in gear["unmatched"]["cameras"]] == ["iPhone 15 Pro"]
    assert [g["gear_id"] for g in gear["active_gear_without_photos"]] == ["g-old"]
    assert (await server.gear_usage(year=2024))["cameras"][0]["photos"] == 1

    dry = await server.mhphotos_sync_gear_usage()
    assert dry["dry_run"] and dry["gear_items"] == 2 and app.gear_usage is None
    done = await server.mhphotos_sync_gear_usage(confirm=True)
    assert done["applied"] and set(app.gear_usage["items"]) == {"g-r5", "g-2470"}
    assert app.gear_usage["items"]["g-r5"]["photos"] == 2


# ---------------------------------------------------------------- spots
async def test_spot_photos_collections_and_sync(app, writes):
    save_snapshot([
        row("p4", "2025-06-12T05:42:10", lat=46.5402, lon=11.6203, r=5),   # ~25 m from the spot
        row("x1", "2025-06-12T06:00:00", lat=46.5410, lon=11.6201, r=2),   # ~100 m, low rating
        row("far", "2025-06-12T07:00:00", lat=46.60, lon=11.70, r=5),
        row("dom", "2024-01-01T10:00:00", lat=51.9626, lon=7.6257, r=1),
    ])
    res = await server.spot_photos(radius_m=300, min_rating=4)
    [seiser, dom] = res["spots_with_own_photos"]["items"]
    assert seiser["spot_id"] == "s-seiser" and seiser["photos"] == 2 and seiser["rated"] == 1
    assert seiser["best"][0]["id"] == "p4" and seiser["best_rating"] == 5
    assert dom["spot_id"] == "s-dom" and dom["rated"] == 0
    assert [s["spot_id"] for s in res["spots_without_good_own_photo"]["items"]] == ["s-dom"]
    assert res["spots_without_position"] == 1

    done = await server.spot_photos_collections(["s-seiser"], confirm=True)
    assert done["applied"] and done["photos_added"] == 1
    [made] = server._adapter.review_collections
    assert (made["root"], made["set"], made["name"], made["photo_ids"]) == ("MHPhotos-Spots", "Spots", "Seiser Alm", ["p4"])

    sync = await server.mhphotos_sync_spot_photos(confirm=True)
    assert sync["applied"] and app.spot_photos["spots"]["s-seiser"] == {
        "photos": 2, "rated": 1, "bestRating": 5, "lastCaptured": "2025-06-12"}


# ---------------------------------------------------------------- trip draft
async def test_trip_outline_and_blog_draft(app):
    out = await server.trip_outline("2025-06-12", "2025-06-12", title="Südtirol")
    [day] = out["days"]
    assert day["photos"] == 2 and day["places"] == ["Seiser Alm", "Brixen"] and day["km"] > 10
    assert day["best"][0]["id"] == "p4"
    assert out["draft_markdown"].startswith("# Südtirol") and "## Tag 1: Seiser Alm → Brixen" in out["draft_markdown"]
    assert "error" in await server.trip_outline("2025-06-13", "2025-06-12")
    coll = await server.trip_outline("2025-06-12", "2025-06-12", collection_id="c1")
    assert "error" in coll, "c1 has no photos of that day"

    dry = await server.mhphotos_create_blog_draft("Südtirol", captured_from="2025-06-12", captured_to="2025-06-12")
    assert dry["dry_run"] and "Seiser Alm" in dry["preview"] and app.blog_posts == []
    done = await server.mhphotos_create_blog_draft("Südtirol", content="# Text", tags=["Reise"], confirm=True)
    assert done["applied"] and done["published"] is False
    assert app.blog_posts[0]["published"] is False and app.blog_posts[0]["tags"] == ["Reise"]


# ---------------------------------------------------------------- offered over HTTP
def test_http_service_offers_new_tools(monkeypatch, app):
    from mcp.server.fastmcp import FastMCP

    srv = FastMCP("t")
    http_service.register_tools(srv, writes=True)
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    for name in ("catalog_audit_overview", "presort_month", "gear_usage", "spot_photos", "presort_apply",
                 "mark_online_in_lightroom", "mhphotos_create_blog_draft"):
        assert name in tools, name
    assert tools["presort_month"].annotations.readOnlyHint is True
    assert tools["presort_apply"].annotations.destructiveHint is False
    assert tools["merge_keywords"].annotations.destructiveHint is True
    assert tools["mhphotos_create_blog_draft"].annotations.readOnlyHint is False
    srv2 = FastMCP("t2")
    http_service.register_tools(srv2, writes=False)
    names = {t.name for t in asyncio.run(srv2.list_tools())}
    assert "presort_apply" not in names and "mhphotos_sync_gear_usage" not in names

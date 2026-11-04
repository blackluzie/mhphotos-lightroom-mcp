"""Catalog audit: wrong date folders, duplicates, review collections."""

import anyio
import pytest
from mhphotos_lightroom_mcp import catalog_audit as ca
from mhphotos_lightroom_mcp import http_service, server

pytestmark = pytest.mark.anyio


def rec(pid, path, t="2021-07-14T10:00:00", **kw):
    return {"id": pid, "path": path, "t": t, "size": 100, "w": 6000, "h": 4000, "fmt": "DNG",
            "cam": "Canon", **kw}


ROOT = "/Volumes/NAS/Fotos/2020-2029"  # decade folder; the photos below are from 2020-2029


@pytest.mark.parametrize(("path", "dates"), [
    (f"{ROOT}/2021/2021-07/2021-07-14/a.dng", ("2021", "2021-07", "2021-07-14")),
    (f"{ROOT}/2021/2021-07/2021-07-14 Hochzeit/a.dng", ("2021", "2021-07", "2021-07-14")),
    (f"{ROOT}/2021/Urlaub/a.dng", ("2021", None, None)),
    (f"{ROOT}/Import/a.dng", (None, None, None)),
    ("/Volumes/NAS/2021-07-14/a.dng", (None, None, "2021-07-14")),
    (f"{ROOT}/2021/20210714_123.dng", ("2021", None, None)),  # file name is not a folder
])
def test_folder_dates(path, dates):
    assert ca.folder_dates(path) == dates


@pytest.mark.parametrize(("path", "taken", "expected"), [
    (f"{ROOT}/2021/2021-07/2021-07-14/a.dng", "2021-07-14T23:59:00", None),
    (f"{ROOT}/2021/2021-07/2021-07-13/a.dng", "2021-07-14T00:30:00", {"level": "day", "day_diff": 1}),
    (f"{ROOT}/2021/2021-06/2021-06-14/a.dng", "2021-07-14T10:00:00", {"level": "month"}),
    (f"{ROOT}/2020/2020-07/2020-07-14/a.dng", "2021-07-14T10:00:00", {"level": "year"}),
    (f"{ROOT}/2021/2020-07/a.dng", "2021-07-14T10:00:00", {"level": "year"}),
    (f"{ROOT}/2021/Urlaub/a.dng", "2021-07-14T10:00:00", {"level": "no_day"}),  # year right, no day folder
    (f"{ROOT}/2021/2021-07/a.dng", "2021-07-14T10:00:00", {"level": "no_day"}),
    ("/Volumes/NAS/Fotos/2021/2021-07/2021-07-14/a.dng", "2021-07-14T10:00:00", {"level": "decade"}),  # no decade
    ("/Volumes/NAS/Fotos/2000-2009/2021/2021-07/2021-07-14/a.dng", "2021-07-14T10:00:00", {"level": "decade"}),
    ("/Volumes/NAS/Fotos/1900-1999/1999/1999-05/1999-05-01/a.dng", "1999-05-01T10:00:00", None),
    ("/Volumes/NAS/Fotos/2010-2019/2019/2019-12/2019-12-31/a.dng", "2019-12-31T23:59:00", None),
])
def test_classify_wrong_folder(path, taken, expected):
    got = ca.classify(rec("x", path, taken), today="2026-11-03")
    if expected is None:
        assert got is None
    else:
        assert got["kind"] == "wrong_folder"
        assert got["expected"] == taken[:10]
        assert {k: got[k] for k in expected} == expected


def test_classify_other_kinds():
    good = f"{ROOT}/2021/2021-07/2021-07-14/a.dng"
    assert ca.classify(rec("x", good, ""), "2026-11-03")["kind"] == "no_date"
    assert ca.classify(rec("x", good, "1970-01-01T00:00:00"), "2026-11-03")["kind"] == "implausible_date"
    assert ca.classify(rec("x", good, "2027-01-01T00:00:00"), "2026-11-03")["kind"] == "implausible_date"
    assert ca.classify(rec("x", f"{ROOT}/Import/a.dng"), "2026-11-03")["kind"] == "unrecognised_folder"


def test_duplicates_are_verified_by_fingerprint(tmp_path):
    a, b, c = tmp_path / "2021-07-14" / "IMG_1.dng", tmp_path / "Import" / "IMG_1.dng", tmp_path / "IMG_1-2.dng"
    for p in (a, b, c):
        p.parent.mkdir(parents=True, exist_ok=True)
    a.write_bytes(b"same" * 1000)
    b.write_bytes(b"same" * 1000)
    c.write_bytes(b"diff" * 1000)  # same size, other content: a burst or a re-save
    an = ca.Analysis(today="2026-11-03")
    an.add(rec("a", str(a), size=4000))
    an.add(rec("b", str(b), size=4000, r=5, edited=True))  # more work in it: keep this one
    an.add(rec("c", str(c), size=4000))
    an.add(rec("v", str(a), size=4000, vc=True))  # virtual copy: never a duplicate
    an.add(rec("raw", str(a), size=4000, fmt="JPG"))  # RAW+JPEG pair: different file type
    an.finish_duplicates()
    [group] = an.findings["duplicates"]
    assert group["verified"] is True
    assert group["keep"]["id"] == "b"
    assert [r["id"] for r in group["remove"]] == ["a"]
    assert an.findings["possible_duplicates"] == []  # c has no partner with other bytes
    assert an.summary()["virtual_copies_skipped"] == 1


def test_resaved_copy_is_a_possible_duplicate(tmp_path):
    an = ca.Analysis(today="2026-11-03")
    an.add(rec("a", f"{tmp_path}/IMG_5.jpg", size=4000, fmt="JPG"))
    an.add(rec("b", f"{tmp_path}/IMG_5-2.jpg", size=4100, fmt="JPG", kw=3))
    an.finish_duplicates()
    [possible] = an.findings["possible_duplicates"]
    assert possible["verified"] is False and possible["keep"]["id"] == "b"


def test_burst_with_other_names_is_not_a_possible_duplicate(tmp_path):
    an = ca.Analysis(today="2026-11-03")
    an.add(rec("a", f"{tmp_path}/IMG_0001.dng", size=4000))
    an.add(rec("b", f"{tmp_path}/IMG_0002.dng", size=4100))
    an.finish_duplicates()
    assert an.findings["duplicates"] == [] and an.findings["possible_duplicates"] == []


def test_missing_file_and_year_scope(tmp_path):
    present = tmp_path / "2021" / "2021-07" / "2021-07-14" / "a.dng"
    present.parent.mkdir(parents=True)
    present.write_bytes(b"x")
    an = ca.Analysis(year=2021, check_files=True, today="2026-11-03")
    an.add(rec("ok", str(present)))
    an.add(rec("gone", str(tmp_path / "2021" / "2021-07" / "2021-07-14" / "b.dng")))
    an.add(rec("other", str(tmp_path / "2019" / "2019-01" / "2019-01-01" / "c.dng"), "2019-01-01T10:00:00"))
    assert [f["id"] for f in an.findings["missing_file"]] == ["gone"]
    assert an.considered == 2 and an.scanned == 3


def test_review_groups_switch_to_months_when_there_are_many_days():
    findings = {k: [] for k in ca.KINDS}
    findings["wrong_folder"] = [{"id": f"p{i}", "expected": f"2021-{1 + i // 28:02d}-{1 + i % 28:02d}"}
                                for i in range(301)]
    groups, mode = ca.review_groups({"findings": findings}, "wrong_folder")
    assert "month" in mode and len(groups) == 11 and groups[0]["name"] == "→ 2021-01"
    findings["wrong_folder"] = findings["wrong_folder"][:3]
    groups, mode = ca.review_groups({"findings": findings}, "wrong_folder")
    assert "day" in mode and [g["name"] for g in groups] == ["→ 2021-01-01", "→ 2021-01-02", "→ 2021-01-03"]


def test_chunk_groups_splits_big_collections():
    groups = [{"set": "S", "name": "big", "photo_ids": [str(i) for i in range(12000)]},
              {"set": "S", "name": "small", "photo_ids": ["a"]}]
    calls = ca.chunk_groups(groups)
    assert [sum(len(g["photo_ids"]) for g in c) for c in calls] == [5000, 5000, 2000 + 1]
    assert all(g["name"] == "big" for g in calls[0])


async def _finish():
    for _ in range(200):
        st = await server.catalog_audit_status()
        if st["status"] != "running":
            return st
        await anyio.sleep(0.01)
    raise AssertionError("audit did not finish")


async def test_tools_end_to_end_on_mock(monkeypatch):
    assert (await server.catalog_audit_results())["error"].startswith("No finished")
    started = await server.catalog_audit_start()
    assert started["started"] is True
    st = await _finish()
    assert st["status"] == "done", st
    summary = st["last_result"]["summary"]
    assert summary["photos_scanned"] == 5 and summary["counts"]["wrong_folder"] == 1
    res = await server.catalog_audit_results("wrong_folder")
    assert res["total"] == 1
    item = res["items"][0]
    assert item["id"] == "p3" and item["level"] == "day" and item["expected"] == "2026-05-02"
    assert item["folder"] == "2026/2026-05/2026-05-01"
    assert (await server.catalog_audit_results("wrong_folder", year=2025))["total"] == 0
    assert "error" in await server.catalog_audit_results("nonsense")

    assert (await server.catalog_audit_collections("wrong_folder", confirm=True))["code"] == "disabled"
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    dry = await server.catalog_audit_collections("wrong_folder")
    assert dry["dry_run"] is True and dry["photos"] == 1 and dry["first_collections"] == ["Ordner falsch › → 2026-05-02 (1)"]
    assert server._adapter.review_collections == []
    done = await server.catalog_audit_collections("wrong_folder", confirm=True)
    assert done["applied"] is True and done["photos_added"] == 1
    [made] = server._adapter.review_collections
    assert (made["root"], made["set"], made["name"], made["photo_ids"]) == ("Prüfen", "Ordner falsch", "→ 2026-05-02", ["p3"])
    assert (await server.catalog_audit_collections("duplicates"))["nothing_to_do"] is True


async def test_only_one_run_at_a_time(monkeypatch):
    gate_open = __import__("threading").Event()
    adapter = server._adapter
    original = adapter.audit_page

    def slow(offset, limit, refresh=False, year=None):
        gate_open.wait(5)
        return original(offset, limit, refresh, year)

    monkeypatch.setattr(adapter, "audit_page", slow)
    assert (await server.catalog_audit_start())["started"] is True
    second = await server.catalog_audit_start(year=2020)
    assert second["started"] is False and "hint" in second
    gate_open.set()
    assert (await _finish())["status"] == "done"


async def test_failed_page_is_retried_then_reported(monkeypatch):
    monkeypatch.setattr(ca, "RETRY_SECONDS", 0)
    calls = []

    def broken(offset, limit, refresh=False, year=None):
        calls.append(offset)
        raise RuntimeError("Lightroom did not answer")

    monkeypatch.setattr(server._adapter, "audit_page", broken)
    await server.catalog_audit_start()
    st = await _finish()
    assert st["status"] == "failed" and "did not answer" in st["error"] and len(calls) == 3


def test_http_service_offers_audit_tools():
    assert {"catalog_audit_start", "catalog_audit_status", "catalog_audit_results"} <= set(http_service.READ_TOOLS)
    assert "catalog_audit_collections" in http_service.WRITE_TOOLS
    assert "catalog_audit_collections" in http_service.ADDITIVE_TOOLS


@pytest.mark.parametrize(("record", "flagged"), [
    ({"path": "/x/Screenshot 2024-01-02 at 10.00.png", "fmt": "PNG", "w": 2880, "h": 1800}, True),
    ({"path": "/x/IMG_0001.PNG", "fmt": "PNG", "w": 1170, "h": 2532}, True),  # iPhone screenshot
    ({"path": "/x/IMG-20210714-WA0003.jpg", "fmt": "JPG", "w": 1600, "h": 1200}, True),
    ({"path": "/x/download.jpg", "fmt": "JPG", "w": 800, "h": 600}, True),
    ({"path": "/x/IMG_1234.HEIC", "fmt": "HEIC", "w": 4032, "h": 3024, "cam": "iPhone 15", "exif": True}, False),
    ({"path": "/x/scan_001.tif", "fmt": "TIFF", "w": 5000, "h": 3500}, False),  # scan: big, no camera only
    ({"path": "/x/clip.mov", "fmt": "VIDEO", "w": 1920, "h": 1080}, False),
])
def test_non_photo(record, flagged):
    assert bool(ca.non_photo_reasons(record)) is flagged


def test_jpeg_next_to_raw_and_reimported_export():
    an = ca.Analysis(today="2026-11-03")
    day = f"{ROOT}/2021/2021-07/2021-07-14"
    an.add(rec("raw", f"{day}/IMG_1.CR3", fmt="RAW"))
    an.add(rec("jpg", f"{day}/IMG_1.JPG", fmt="JPG"))
    an.add(rec("exp", f"{ROOT}/Exporte/IMG_1-2.jpg", fmt="JPG", w=2048, h=1365))
    an.add(rec("raw2", f"{day}/IMG_2.CR3", fmt="RAW", t="2021-07-14T10:00:01"))
    an.add(rec("jpg2", f"{day}/IMG_2.JPG", fmt="JPG", t="2021-07-14T10:00:01", r=4))  # work only on the JPEG
    an.add(rec("other", f"{day}/IMG_3.JPG", fmt="JPG"))  # same second, other name: a different shot
    an.finish()
    pairs = {f["id"]: f for f in an.findings["jpeg_with_raw"]}
    assert set(pairs) == {"jpg", "jpg2"}
    assert pairs["jpg"]["raw"]["id"] == "raw" and "check" not in pairs["jpg"]
    assert pairs["jpg2"]["check"] is True
    assert [f["id"] for f in an.findings["reimported_export"]] == ["exp"]
    groups, _ = ca.review_groups({"findings": an.findings}, "jpeg_with_raw")
    assert [(g["name"], g["photo_ids"]) for g in groups] == [
        ("JPEG neben RAW – entfernen", ["jpg"]), ("JPEG neben RAW – erst prüfen", ["jpg2"])]
    assert an.summary()["counts"]["jpeg_with_raw_to_check"] == 1
    assert an.findings["duplicates"] == []  # a RAW+JPEG pair is not a duplicate


def test_old_result_files_without_new_kinds_still_work():
    groups, _ = ca.review_groups({"findings": {"duplicates": []}}, "non_photo")
    assert groups[0]["photo_ids"] == []


async def test_year_run_reads_only_that_year(monkeypatch):
    seen = []
    original = server._adapter.audit_page

    def spy(offset, limit, refresh=False, year=None):
        seen.append(year)
        return original(offset, limit, refresh, year)

    monkeypatch.setattr(server._adapter, "audit_page", spy)
    await server.catalog_audit_start(year=2026)
    st = await _finish()
    assert st["status"] == "done" and seen and set(seen) == {2026}
    assert 0 < st["read"] < len(server._adapter._photos) or st["read"] == st["total"]
    assert all(str(r["t"])[:4] == "2026" for r in server._adapter.audit_page(0, 100, False, 2026)["photos"])


async def test_year_run_scans_in_steps_and_reports_progress(monkeypatch):
    adapter = server._adapter
    original = adapter.audit_scan
    steps, release = [], __import__("threading").Event()

    def scan(year, start):
        steps.append(start)
        if len(steps) == 2:
            release.wait(5)
        return original(year, start)

    monkeypatch.setattr(adapter, "audit_scan", scan)
    await server.catalog_audit_start(year=2026)
    for _ in range(200):
        st = await server.catalog_audit_status()
        if len(steps) >= 2:
            break
        await anyio.sleep(0.01)
    assert st["phase"] == "scanning catalog by capture year"
    assert st["scanned"] == 2 and st["total_photos"] == 5 and st["percent"] == 40.0
    release.set()
    done = await _finish()
    assert done["status"] == "done" and steps == [0, 2, 4]


async def test_cancel_stops_a_run_and_allows_a_new_one(monkeypatch):
    assert (await server.catalog_audit_cancel())["cancelled"] is False
    gate = __import__("threading").Event()
    original = server._adapter.audit_page

    def slow(offset, limit, refresh=False, year=None):
        gate.wait(5)
        return original(offset, limit, refresh, year)

    monkeypatch.setattr(server._adapter, "audit_page", slow)
    assert (await server.catalog_audit_start())["started"] is True
    cancelled = await server.catalog_audit_cancel()
    assert cancelled["cancelled"] is True and cancelled["cancel_requested"] is True
    gate.set()
    st = await _finish()
    assert st["status"] == "cancelled", st
    monkeypatch.setattr(server._adapter, "audit_page", original)
    assert (await server.catalog_audit_start())["started"] is True
    assert (await _finish())["status"] == "done"


async def test_cancel_during_year_scan(monkeypatch):
    gate = __import__("threading").Event()
    original = server._adapter.audit_scan

    def scan(year, start):
        gate.wait(5)
        return original(year, start)

    monkeypatch.setattr(server._adapter, "audit_scan", scan)
    await server.catalog_audit_start(year=2026)
    await server.catalog_audit_cancel()
    gate.set()
    assert (await _finish())["status"] == "cancelled"


def test_classify_reports_the_full_target_path():
    got = ca.classify(rec("x", "/Volumes/NAS/Fotos/2021/2021-07/2021-07-14/a.dng", "2026-10-01T08:00:00"), "2026-11-03")
    assert got["level"] == "year" and got["expected_path"] == "2020-2029/2026/2026-10/2026-10-01"


@pytest.mark.parametrize(("year", "folder"), [(1985, "1900-1999"), (1999, "1900-1999"), (2000, "2000-2009"),
                                              (2019, "2010-2019"), (2026, "2020-2029")])
def test_decade_folder(year, folder):
    assert ca.decade_folder(year) == folder


@pytest.mark.parametrize(("name", "expected"), [
    ("2019-12-08_19-35-23_000.jpeg", "2019-12-08T19:35:23"),
    ("IMG_20191208_193523.jpg", "2019-12-08T19:35:23"),
    ("20191208.png", "2019-12-08"),
    ("2019-11-17_16-32-47_000 (2020-01-20T05_55_48.164).jpeg", "2019-11-17T16:32:47"),
    ("IMG_3259.MP4", None),
    ("2019-13-45_x.jpg", None),  # not a date
    ("DSC_4711.jpg", None),
    ("2031-01-01_x.jpg", None),  # in the future
])
def test_name_date(name, expected):
    assert ca.name_date("/x/" + name, today="2026-11-03") == expected


def test_videos_and_dates_in_names_are_not_plain_no_date():
    good = f"{ROOT}/2020/2020-01/2020-01-04/"
    assert ca.classify(rec("v", good + "IMG_3259.MP4", "", fmt="VIDEO"), "2026-11-03")["kind"] == "video_no_date"
    got = ca.classify(rec("n", good + "2020-01-04_23-25-28_000.jpeg", "", fmt="JPG"), "2026-11-03")
    assert got == {"kind": "date_in_name", "name_date": "2020-01-04T23:25:28", "folder_fits": True}
    other = ca.classify(rec("n", f"{ROOT}/2020/2020-01/2020-01-05/2020-01-04_23-25-28.jpeg", "", fmt="JPG"), "2026-11-03")
    assert other["folder_fits"] is False
    assert ca.classify(rec("p", good + "IMG_1.png", "", fmt="PNG"), "2026-11-03")["kind"] == "no_date"


def test_near_duplicates_are_split_from_possible_ones(tmp_path):
    an = ca.Analysis(today="2026-11-03")
    an.add(rec("a", f"{tmp_path}/IMG_7.jpg", size=4_648_733, fmt="JPG"))
    an.add(rec("b", f"{tmp_path}/IMG_7-2.jpg", size=4_649_752, fmt="JPG"))  # 0.02 % apart: same image
    an.add(rec("c", f"{tmp_path}/IMG_8.jpg", "2021-07-14T10:00:01", size=4_000, fmt="JPG"))
    an.add(rec("d", f"{tmp_path}/IMG_8-2.jpg", "2021-07-14T10:00:01", size=4_300, fmt="JPG"))  # 7 % apart
    an.finish_duplicates()
    [near] = an.findings["near_duplicates"]
    assert {near["keep"]["id"], near["remove"][0]["id"]} == {"a", "b"}
    [possible] = an.findings["possible_duplicates"]
    assert {possible["keep"]["id"], possible["remove"][0]["id"]} == {"c", "d"}
    summary = an.summary()
    assert summary["counts"]["near_duplicates_to_remove"] == 1 and "near_duplicates" in summary["storage"]["freeable_gb"]


def test_review_groups_filter_by_level_and_cover_near_duplicates():
    findings = {k: [] for k in ca.KINDS}
    findings["wrong_folder"] = [
        {"id": "p1", "expected": "2021-07-14", "level": "decade"},
        {"id": "p2", "expected": "2021-07-14", "level": "no_day"},
        {"id": "p3", "expected": "2021-07-15", "level": "day"}]
    groups, _ = ca.review_groups({"findings": findings}, "wrong_folder", level="decade")
    assert [(g["name"], g["photo_ids"]) for g in groups] == [("→ 2021-07-14", ["p1"])]
    findings["near_duplicates"] = [{"keep": {"id": "k"}, "remove": [{"id": "r"}]}]
    groups, _ = ca.review_groups({"findings": findings}, "near_duplicates")
    assert [(g["name"], g["photo_ids"]) for g in groups] == [
        ("nahezu gleich – entfernen", ["r"]), ("nahezu gleich – behalten", ["k"])]
    assert ca.review_groups({"findings": findings}, "video_no_date")[0][0]["set"] == "Sonstiges"

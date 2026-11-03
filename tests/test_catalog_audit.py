"""Catalog audit: wrong date folders, duplicates, review collections."""

import anyio
import pytest
from mhphotos_lightroom_mcp import catalog_audit as ca
from mhphotos_lightroom_mcp import http_service, server

pytestmark = pytest.mark.anyio


def rec(pid, path, t="2021-07-14T10:00:00", **kw):
    return {"id": pid, "path": path, "t": t, "size": 100, "w": 6000, "h": 4000, "fmt": "DNG",
            "cam": "Canon", **kw}


ROOT = "/Volumes/NAS/Fotos"


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
    (f"{ROOT}/2021/Urlaub/a.dng", "2021-07-14T10:00:00", None),  # only the year is known and right
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

    def slow(offset, limit, refresh=False):
        gate_open.wait(5)
        return original(offset, limit, refresh)

    monkeypatch.setattr(adapter, "audit_page", slow)
    assert (await server.catalog_audit_start())["started"] is True
    second = await server.catalog_audit_start(year=2020)
    assert second["started"] is False and "hint" in second
    gate_open.set()
    assert (await _finish())["status"] == "done"


async def test_failed_page_is_retried_then_reported(monkeypatch):
    monkeypatch.setattr(ca, "RETRY_SECONDS", 0)
    calls = []

    def broken(offset, limit, refresh=False):
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

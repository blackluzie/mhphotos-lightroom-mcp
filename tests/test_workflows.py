"""Photos by date, place suggestions, Instagram week plan, storage balance."""

import asyncio

import pytest
from mcp.server.fastmcp import FastMCP
from mhphotos_lightroom_mcp import catalog_audit as ca
from mhphotos_lightroom_mcp import http_service, server
from mhphotos_lightroom_mcp.adapters import MockAdapter

from .fake_mhphotos import FakeMHPhotos

pytestmark = pytest.mark.anyio


async def test_photos_by_date_filters_and_counts():
    res = await server.photos_by_date("2026-01-01", "2026-12-31")
    assert res["counts"]["in_range"] == 3 and [p["id"] for p in res["photos"]] == ["p1", "p2", "p3"]
    assert (await server.photos_by_date("2026-01-01", "2026-12-31", min_rating=4))["photos"][0]["id"] == "p1"
    assert [p["id"] for p in (await server.photos_by_date("2025-01-01", "2026-12-31", unreviewed=True))["photos"]] == ["p5"]
    assert [p["id"] for p in (await server.photos_by_date("2025-01-01", "2026-12-31", max_rating=1))["photos"]] == ["p5", "p3"]
    no_gps = await server.photos_by_date("2025-01-01", "2026-12-31", missing="gps")
    assert [p["id"] for p in no_gps["photos"]] == ["p2", "p3"]
    page = await server.photos_by_date("2025-01-01", "2026-12-31", limit=2)
    assert page["next_offset"] == 2
    assert "error" in await server.photos_by_date("2026", "2026-12-31")
    assert "error" in await server.photos_by_date("2026-12-31", "2026-01-01")
    assert "error" in await server.photos_by_date("2026-01-01", "2026-12-31", missing="color")


async def test_suggest_locations_uses_nearest_photo_with_gps():
    res = await server.suggest_locations("2026-05-01", "2026-05-01", max_minutes=5)
    [group] = res["groups"]
    assert group["photo_ids"] == ["p2"] and group["from_photo"] == "IMG_0001.dng"
    assert group["latitude"] == 51.96 and group["max_minutes_apart"] == 2
    assert res["photos_without_gps"] == 1
    # the camera clock ran 10 minutes slow: without correction nothing is within 5 minutes
    shifted = await server.suggest_locations("2026-05-01", "2026-05-01", max_minutes=5, clock_offset_minutes=10)
    assert shifted["groups"] == []


async def test_set_location_is_a_gated_write(monkeypatch):
    assert (await server.set_location(["p2"], 51.0, 7.0, confirm=True))["code"] == "disabled"
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    assert "error" in await server.set_location(["p2"], 51.0)
    assert "error" in await server.set_location(["p2"])
    dry = await server.set_location(["p2"], 51.96, 7.63, city="Münster")
    assert dry["dry_run"] is True
    done = await server.set_location(["p2"], 51.96, 7.63, city="Münster", country="Deutschland", confirm=True)
    assert done["applied"] is True and done["gps_written"] is True
    meta = await server.get_photo_metadata("p2")
    assert meta["gps"] == {"latitude": 51.96, "longitude": 7.63} and meta["location"] == "Münster, Deutschland"


@pytest.fixture
def app(monkeypatch):
    f = FakeMHPhotos()
    monkeypatch.setenv("MHPHOTOS_INBOX_URL", f.url)
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "secret")
    yield f
    f.stop()


async def test_instagram_candidates_skip_posted_and_marked(app):
    class Rated(MockAdapter):
        def photos_by_date(self, captured_from, captured_to, offset, limit, refresh=False):
            recs = [{"id": f"x{i}", "file": f"IMG_{i}.dng", "t": f"2025-06-0{i}T10:00:00", "r": 5} for i in range(1, 6)]
            recs[0]["insta"] = True  # already marked as posted in Lightroom
            recs[1]["pick"] = -1  # rejected
            recs.append({"id": "low", "t": "2025-06-09T10:00:00", "r": 2})
            return {"total": len(recs), "offset": 0, "next_offset": len(recs), "photos": recs}

        def get_preview(self, photo_id, size):
            return super().get_preview("p1", size)

    server.set_adapter(Rated())
    app.duplicate_after = 1  # first checked photo is new, the next ones count as posted
    res = await server.instagram_candidates(count=3, captured_from="2025-01-01", captured_to="2025-12-31")
    assert res["pool"] == 3 and res["checked"] == 3
    assert len(res["candidates"]) == 1 and res["already_planned_or_posted"] == 2
    assert res["candidates"][0]["id"] in {"x3", "x4", "x5"}
    again = await server.instagram_candidates(count=3, captured_from="2025-01-01", captured_to="2025-12-31")
    assert again["pool"] == 3  # deterministic within a week
    assert "error" in await server.instagram_candidates(account="nobody")


async def test_best_times_passes_the_app_answer_through(app):
    res = await server.mhphotos_best_times("malou")
    assert res["account"] == "malou" and res["slots"][0]["hour"] == 19


def test_instagram_planning_over_http_only_when_enabled(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_INBOX_URL", "https://mhphotos.example")
    monkeypatch.delenv(http_service.INSTAGRAM_FLAG, raising=False)
    app = FastMCP("a")
    http_service.register_tools(app, writes=True)
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert {"instagram_candidates", "mhphotos_best_times", "mhphotos_instagram_queue"} <= names
    assert not set(http_service.INSTAGRAM_TOOLS) & names
    monkeypatch.setenv(http_service.INSTAGRAM_FLAG, "1")
    app = FastMCP("b")
    http_service.register_tools(app, writes=False)
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    assert set(http_service.INSTAGRAM_TOOLS) <= set(tools)
    assert tools["mhphotos_schedule_instagram"].annotations.openWorldHint is True
    assert tools["mhphotos_schedule_instagram"].annotations.readOnlyHint is False
    assert tools["mhphotos_cancel_instagram"].annotations.destructiveHint is True
    monkeypatch.delenv("MHPHOTOS_INBOX_URL")
    app = FastMCP("c")
    http_service.register_tools(app, writes=False)
    assert not set(http_service.INSTAGRAM_TOOLS) & {t.name for t in asyncio.run(app.list_tools())}


def test_consent_names_instagram_planning(monkeypatch):
    from mhphotos_lightroom_mcp.oauth_http import consent_text
    monkeypatch.delenv(http_service.INSTAGRAM_FLAG, raising=False)
    assert "Instagram" not in consent_text()
    monkeypatch.setenv(http_service.INSTAGRAM_FLAG, "1")
    assert "automatisch auf Instagram" in consent_text()


def test_storage_balance():
    an = ca.Analysis(today="2026-11-03")
    gb = 1024 ** 3
    root = "/Volumes/NAS/Fotos/2021/2021-07/2021-07-14"
    an.add({"id": "raw", "path": f"{root}/IMG_1.CR3", "t": "2021-07-14T10:00:00", "size": 3 * gb, "fmt": "RAW",
            "cam": "C", "exif": True, "w": 6000, "h": 4000})
    an.add({"id": "jpg", "path": f"{root}/IMG_1.JPG", "t": "2021-07-14T10:00:00", "size": gb, "fmt": "JPG",
            "cam": "C", "exif": True, "w": 6000, "h": 4000})
    an.add({"id": "shot", "path": f"{root}/Screenshot 1.png", "t": "2021-07-14T11:00:00", "size": gb // 2,
            "fmt": "PNG", "w": 1170, "h": 2532})
    an.finish()
    storage = an.summary()["storage"]
    assert storage["total_gb"] == 4.5 and storage["by_year_gb"] == {"2021": 4.5}
    assert storage["freeable_gb"]["jpeg_with_raw"] == 1.0 and storage["freeable_gb"]["non_photo"] == 0.5
    assert storage["freeable_gb"]["sum"] == 1.5


FAKE_EXIFTOOL = """#!{python}
import sys
args = sys.argv[1:]
path = args[-1]
side = path + ".exif"
if "-s3" in args:
    try:
        print(open(side).read(), end="")
    except OSError:
        pass
    sys.exit(0)
if "boom" in path:
    print("Error: cannot write", file=sys.stderr)
    open(path, "wb").write(b"half written")
    sys.exit(1)
value = [a for a in args if a.startswith("-DateTimeOriginal=")][0].split("=", 1)[1]
open(side, "w").write(value)
print("    1 image files updated")
"""


def test_file_edits_over_http_only_when_enabled(monkeypatch):
    monkeypatch.delenv(http_service.FILES_FLAG, raising=False)
    app = FastMCP("a")
    http_service.register_tools(app, writes=True)
    assert not set(http_service.FILES_TOOLS) & {t.name for t in asyncio.run(app.list_tools())}
    monkeypatch.setenv(http_service.FILES_FLAG, "1")
    app = FastMCP("b")
    http_service.register_tools(app, writes=False)  # file edits need the edit opt-in as well
    assert not set(http_service.FILES_TOOLS) & {t.name for t in asyncio.run(app.list_tools())}
    app = FastMCP("c")
    http_service.register_tools(app, writes=True)
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    assert tools["write_name_dates_to_files"].annotations.destructiveHint is True


@pytest.mark.parametrize(("iso", "expected"), [("2019-09-10T08:24:16", "2019:09:10 08:24:16")])
def test_exif_time(iso, expected):
    from mhphotos_lightroom_mcp import file_dates
    assert file_dates.exif_time(iso) == expected


def test_check_file_refuses_unsafe_targets(tmp_path):
    from mhphotos_lightroom_mcp import file_dates
    good = tmp_path / "a.jpeg"
    good.write_bytes(b"x")
    assert file_dates.check_file(str(good), "a.jpeg") is None
    assert file_dates.check_file(str(good), "b.jpeg") == "Pfad passt nicht zum Dateinamen"
    assert file_dates.check_file(None, "a.jpeg") == "Lightroom nennt keinen Pfad"
    assert file_dates.check_file("a.jpeg", "a.jpeg") == "Pfad passt nicht zum Dateinamen"  # not absolute
    png = tmp_path / "a.png"
    png.write_bytes(b"x")
    assert file_dates.check_file(str(png), "a.png") == "kein JPEG"
    assert file_dates.check_file(str(tmp_path / "gone.jpeg"), "gone.jpeg") == "Datei nicht gefunden"
    empty = tmp_path / "e.jpg"
    empty.write_bytes(b"")
    assert file_dates.check_file(str(empty), "e.jpg") == "Datei ist leer"
    link = tmp_path / "l.jpg"
    link.symlink_to(good)
    assert file_dates.check_file(str(link), "l.jpg") == "Datei nicht gefunden"


async def test_write_name_dates_to_files_end_to_end(monkeypatch, tmp_path):
    import stat
    import sys

    from mhphotos_lightroom_mcp import file_dates
    monkeypatch.setenv("MHPHOTOS_CONFIG_DIR", str(tmp_path / "cfg"))
    fake = tmp_path / "exiftool"
    fake.write_text(FAKE_EXIFTOOL.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(file_dates, "exiftool_path", lambda: str(fake))
    pics = tmp_path / "Photos" / "2020-2029" / "2020" / "2020-01"
    pics.mkdir(parents=True)
    files = {"p2": "2019-12-08_19-35-23_000.jpeg", "p3": "2019-12-09_10-00-00_000.jpeg",
             "p5": "2019-12-10_11-00-00_000.jpeg", "p1": "boom_2019-12-11_12-00-00.jpeg"}
    for name in files.values():
        (pics / name).write_bytes(b"original " + name.encode())
    (pics / (files["p3"] + ".exif")).write_text("2019:12:09 10:00:00")  # already has a capture time
    findings = {k: [] for k in ca.KINDS}
    findings["date_in_name"] = [
        {"id": pid, "file": name, "folder": "2020-2029/2020/2020-01", "name_date": n_date,
         "folder_fits": False}
        for (pid, name), n_date in zip(files.items(), ["2019-12-08T19:35:23", "2019-12-09T10:00:00",
                                                       "2019-12-10T11:00:00", "2019-12-11T12:00:00"], strict=True)]
    ca.save_result({"finished_at": "x", "findings": findings, "summary": {}})
    monkeypatch.setattr(server._adapter, "photo_paths", lambda ids: {"paths": {
        i: str(pics / files[i]) for i in ids if i in files}})

    assert (await server.write_name_dates_to_files(confirm=True))["code"] == "disabled"
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    assert (await server.write_name_dates_to_files(confirm=True))["code"] == "disabled"  # needs its own opt-in
    monkeypatch.setenv("MHPHOTOS_ALLOW_FILE_EDITS", "1")

    dry = await server.write_name_dates_to_files()  # limit defaults to 1: the first photo only
    assert dry["dry_run"] is True and dry["this_call"] == 1
    assert dry["will_write"] == [{"file": files["p2"], "folder": "2020-2029/2020/2020-01", "set_to": "2019-12-08T19:35:23"}]
    assert not (pics / (files["p2"] + ".exif")).exists()  # nothing changed

    done = await server.write_name_dates_to_files(confirm=True)
    assert done["applied"] is True and done["written"] == 1 and done["failed"] == []
    assert (pics / (files["p2"] + ".exif")).read_text() == "2019:12:08 19:35:23"
    backups = list((tmp_path / "cfg" / "file-backups").rglob("*.jpeg"))
    assert [b.read_bytes() for b in backups] == [b"original " + files["p2"].encode()]
    assert done["collection"].endswith("Datum in Datei geschrieben")
    assert server._adapter.review_collections[-1]["photo_ids"] == ["p2"]

    rest = await server.write_name_dates_to_files(limit=10, confirm=True)
    assert rest["written"] == 1 and [f["file"] for f in rest["failed"]] == [files["p1"]]
    # the one written before and the one that had a time already are left alone
    assert {s["file"] for s in rest["skipped"]} == {files["p2"], files["p3"]}
    assert (pics / files["p1"]).read_bytes() == b"original " + files["p1"].encode()  # restored after the failure

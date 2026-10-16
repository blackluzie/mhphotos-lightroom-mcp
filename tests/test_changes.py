"""Collections, labels, title/caption, virtual copies, audit log, undo, approval, sync."""

import json
import os
import stat

import pytest
from mhphotos_lightroom_mcp import audit, gate, publishing, server

from .fake_mhphotos import FakeMHPhotos

pytestmark = pytest.mark.anyio


@pytest.fixture
def writes(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")


def raw(pid):
    return server._adapter._photos[pid]


# ---- collections -----------------------------------------------------------
async def test_collections_need_opt_in():
    assert (await server.create_collection("X", confirm=True))["code"] == "disabled"
    assert (await server.add_to_collection("c1", ["p4"], confirm=True))["code"] == "disabled"


async def test_create_collection_flow(writes):
    dry = await server.create_collection("Südtirol 09 2026")
    assert dry["dry_run"] and len((await server.list_collections())["collections"]) == 3
    done = await server.create_collection("Südtirol 09 2026", confirm=True)
    assert done["applied"] and done["name"] == "Südtirol 09 2026"
    again = await server.create_collection("Südtirol 09 2026", confirm=True)
    assert again["exists"] is True and again["id"] == done["id"]  # no second album, no approval needed
    assert "error" in await server.create_collection("  ")


async def test_add_to_collection(writes):
    made = await server.create_collection("Auswahl", confirm=True)
    dry = await server.add_to_collection(made["id"], ["p4", "p5"])
    assert dry["dry_run"] and server._adapter._collections[-1].photo_ids == ()
    done = await server.add_to_collection(made["id"], ["p4", "p5"], confirm=True)
    assert done["applied"] and server._adapter._collections[-1].photo_ids == ("p4", "p5")
    assert "error" in await server.add_to_collection("nope", ["p4"], confirm=True)
    assert "error" in await server.add_to_collection(made["id"], ["p4", "ghost"], confirm=True)


# ---- label / title / caption ----------------------------------------------
async def test_color_label(writes):
    assert "error" in await server.set_color_label(["p4"], "pink", confirm=True)
    assert (await server.set_color_label(["p4"], "green", confirm=True))["applied"]
    assert (await server.get_photo_metadata("p4"))["color_label"] == "green"


async def test_title_caption_guard_and_overwrite(writes):
    assert (await server.set_title_caption(["p4"], title="Seiser Alm", confirm=True))["applied"]
    meta = await server.get_photo_metadata("p4")
    assert meta["title"] == "Seiser Alm" and "caption" not in meta
    clash = await server.set_title_caption(["p4"], title="Neu", confirm=True)
    assert "overwrite" in clash["error"] and clash["photos_with_text"][0]["title"] == "Seiser Alm"
    assert (await server.get_photo_metadata("p4"))["title"] == "Seiser Alm"
    assert (await server.set_title_caption(["p4"], title="Neu", overwrite=True, confirm=True))["applied"]
    assert (await server.get_photo_metadata("p4"))["title"] == "Neu"
    assert "error" in await server.set_title_caption(["p4"])
    assert "error" in await server.set_title_caption(["p4"], title="x" * 201)


async def test_virtual_copies(writes):
    dry = await server.create_virtual_copies(["p4"])
    assert dry["dry_run"] and "p4-vc1" not in server._adapter._photos
    done = await server.create_virtual_copies(["p4"], confirm=True)
    assert done["copies"] == [{"source_id": "p4", "id": "p4-vc1"}]
    assert (await server.get_photo_metadata("p4-vc1"))["virtual_copy_of"] == "p4"
    assert "error" in await server.create_virtual_copies([f"p{i}" for i in range(11)], confirm=True)


# ---- audit log -------------------------------------------------------------
async def test_audit_log_records_confirmed_changes_only(writes):
    await server.set_rating(["p2"], 1)  # dry run: not logged
    assert audit.read_all() == []
    done = await server.set_rating(["p2"], 1, confirm=True)
    entries = audit.read_all()
    assert len(entries) == 1 and entries[0]["id"] == done["change_id"]
    e = entries[0]
    assert e["tool"] == "set_rating" and e["photo_ids"] == ["p2"] and e["before"] == {"p2": {"rating": 3}}
    assert e["undoable"] is True and e["approved_by"] == "flag"
    mode = stat.S_IMODE(os.stat(audit.log_path()).st_mode)
    assert mode & 0o077 == 0  # owner only


async def test_audit_log_survives_damaged_line_and_lists(writes):
    await server.set_flag(["p2"], "picked", confirm=True)
    with audit.log_path().open("a") as fh:
        fh.write("not json\n")
    await server.set_flag(["p3"], "none", confirm=True)
    res = await server.list_changes(limit=5)
    assert [c["tool"] for c in res["changes"]] == ["set_flag", "set_flag"]
    assert all(c["undoable"] for c in res["changes"])


async def test_no_secrets_in_audit_log(writes, monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "tok-xyz-123")
    await server.set_rating(["p2"], 2, confirm=True)
    assert "tok-xyz-123" not in audit.log_path().read_text()


# ---- undo ------------------------------------------------------------------
async def test_undo_nothing_and_disabled():
    assert (await server.undo_last_change(confirm=True))["code"] == "disabled"


async def test_undo_nothing_to_undo(writes):
    assert "Nothing to undo" in (await server.undo_last_change())["error"]


async def test_undo_rating_flag_color_title_keywords(writes):
    await server.set_rating(["p2", "p3"], 5, confirm=True)  # p2 was 3, p3 was 1
    dry = await server.undo_last_change()
    assert dry["dry_run"] and raw("p2").rating == 5  # preview changes nothing
    assert (await server.undo_last_change(confirm=True))["restored"] == 2
    assert (raw("p2").rating, raw("p3").rating) == (3, 1)

    await server.set_flag(["p2"], "picked", confirm=True)
    await server.undo_last_change(confirm=True)
    assert raw("p2").flag == "none"

    await server.set_color_label(["p2"], "red", confirm=True)
    await server.undo_last_change(confirm=True)
    assert raw("p2").extra["color_label"] == "none"

    await server.set_title_caption(["p2"], title="T", caption="C", confirm=True)
    await server.undo_last_change(confirm=True)
    assert raw("p2").extra["title"] == "" and raw("p2").extra["caption"] == ""

    await server.add_keywords(["p2"], ["portrait", "neu"], confirm=True)  # 'portrait' was already there
    await server.undo_last_change(confirm=True)
    assert raw("p2").keywords == ("portrait",)  # only the keyword WE added is removed


async def test_undo_is_a_stack_and_logged(writes):
    await server.set_rating(["p2"], 5, confirm=True)
    await server.set_rating(["p2"], 4, confirm=True)
    first = await server.undo_last_change(confirm=True)
    assert raw("p2").rating == 5 and first["tool"] == "set_rating"
    second = await server.undo_last_change(confirm=True)
    assert raw("p2").rating == 3
    assert "Nothing to undo" in (await server.undo_last_change(confirm=True))["error"]
    log = audit.read_all()
    assert [e["tool"] for e in log] == ["set_rating", "set_rating", "undo_last_change", "undo_last_change"]
    assert second["change_id"] != first["change_id"]


async def test_undo_skips_photos_changed_since(writes):
    await server.set_rating(["p2", "p3"], 5, confirm=True)
    server._adapter._photos["p3"] = raw("p3").__class__(**{**raw("p3").__dict__, "rating": 2})  # edited elsewhere
    res = await server.undo_last_change(confirm=True)
    assert res["restored"] == 1 and res["skipped"][0]["id"] == "p3"
    assert raw("p2").rating == 3 and raw("p3").rating == 2


async def test_undo_everything_changed_since(writes):
    await server.set_rating(["p2"], 5, confirm=True)
    await server.set_rating(["p2"], 4, confirm=True)
    server._adapter._photos["p2"] = raw("p2").__class__(**{**raw("p2").__dict__, "rating": 1})
    assert "No photo can be restored" in (await server.undo_last_change(confirm=True))["error"]


async def test_non_undoable_changes_are_not_undone(writes):
    await server.apply_develop_settings(["p4"], {"Exposure2012": 0.5}, confirm=True)
    await server.create_collection("A", confirm=True)
    assert "Nothing to undo" in (await server.undo_last_change())["error"]
    await server.set_rating(["p2"], 5, confirm=True)
    await server.apply_preset(["p4"], "Vivid", confirm=True)  # later, but not undoable
    assert (await server.undo_last_change(confirm=True))["tool"] == "set_rating"


# ---- approval gate ---------------------------------------------------------
async def test_gate_modes(monkeypatch):
    assert await gate.approve(None, "x") == (True, "flag")  # direct call
    monkeypatch.setenv("MHPHOTOS_CONFIRMATION", "bogus")
    assert gate.mode() == "ask"
    monkeypatch.setenv("MHPHOTOS_CONFIRMATION", "REQUIRE")
    assert gate.mode() == "require"
    # strict: with nobody to ask (direct call / `call` command) a confirmed change is refused
    assert await gate.approve(None, "x") == (False, "unsupported")
    monkeypatch.setenv("MHPHOTOS_CONFIRMATION", "flag")
    assert await gate.approve(None, "x") == (True, "flag")
    assert gate.refusal("declined")["declined"] is True
    assert gate.refusal("unsupported")["code"] == "approval_unavailable"
    assert gate.refusal("failed")["code"] == "approval_failed"


# ---- sync develop settings ---------------------------------------------------
async def test_sync_develop_settings(writes):
    a = server._adapter._develop
    a["p4"].update({"Exposure2012": 0.4, "Contrast2012": 12, "Temperature": 5200, "Tint": 3, "CameraProfile": "Adobe"})
    dry = await server.sync_develop_settings("p4", ["p5"])
    assert dry["dry_run"] and set(dry["new"]) == {"Exposure2012", "Contrast2012"}  # no WB, no info keys
    assert dry["from"] == "p4" and a["p5"]["Exposure2012"] == 0.0
    done = await server.sync_develop_settings("p4", ["p5"], confirm=True)
    assert done["applied"] and a["p5"]["Contrast2012"] == 12 and "Temperature" not in a["p5"]
    wb = await server.sync_develop_settings("p4", ["p1"], include_white_balance=True, confirm=True)
    assert a["p1"]["Temperature"] == 5200 and wb["applied"]
    only = await server.sync_develop_settings("p4", ["p2"], keys=["Contrast2012"], confirm=True)
    assert set(only["new"]) == {"Contrast2012"}
    assert "error" in await server.sync_develop_settings("p4", ["p4"])
    assert "error" in await server.sync_develop_settings("p4", ["p2"], keys=["Bogus"])
    assert "error" in await server.sync_develop_settings("p1", ["p2"], keys=["Sharpness"])  # p1 has none... or uses source


# ---- inbox marker + publish audit --------------------------------------------
async def test_send_marks_photos_in_lightroom(writes, monkeypatch, tmp_path):
    app = FakeMHPhotos()
    monkeypatch.setattr(publishing, "upload", lambda cfg, path, meta: {"status": "uploaded", "inbox_id": "lr-x"})
    try:
        monkeypatch.setenv("MHPHOTOS_INBOX_URL", app.url)
        monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "secret")
        monkeypatch.setenv("MHPHOTOS_EXPORT_DIR", str(tmp_path / "exp"))
        res = await server.send_to_mhphotos_inbox(["p4"], confirm=True, mark_in_lightroom=True)
        assert res["marked"] == {"keyword": "MHPhotos-Inbox", "photos": 1}
        assert "MHPhotos-Inbox" in (await server.get_photo_metadata("p4"))["keywords"]
        monkeypatch.delenv("MHPHOTOS_ALLOW_WRITES")
        assert (await server.send_to_mhphotos_inbox(["p5"], confirm=True, mark_in_lightroom=True))["code"] == "disabled"
        assert "send_to_mhphotos_inbox" in [e["tool"] for e in audit.read_all()]
    finally:
        app.stop()


async def test_publish_is_audited(monkeypatch):
    app = FakeMHPhotos()
    try:
        monkeypatch.setenv("MHPHOTOS_INBOX_URL", app.url)
        monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "secret")
        monkeypatch.setenv("MHPHOTOS_ALLOW_PUBLISH", "1")
        res = await server.mhphotos_publish(["lr-1"], confirm=True)
        entry = audit.read_all()[-1]
        assert entry["tool"] == "mhphotos_publish" and entry["id"] == res["change_id"]
        assert entry["change"]["items"][0]["title"] == "Abendlicht am Aasee"
        none = await server.mhphotos_publish(["lr-3"], confirm=True)  # already public: nothing to approve
        assert none["published"] == 0 and "change_id" not in none
    finally:
        app.stop()


def test_audit_entry_is_valid_json_lines(tmp_path):
    audit.record("t", ["a"], {"x": 1}, before={"a": {"v": 1}}, undoable=True)
    audit.record("u", [], {}, approved_by="user", undoes="zzz")
    lines = audit.log_path().read_text().splitlines()
    assert all(isinstance(json.loads(line), dict) for line in lines) and len(lines) == 2

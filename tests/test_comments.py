"""Instagram comments: read the MHPhotos comment inbox, redraft, reply/like only after confirm."""

import pytest
from mhphotos_lightroom_mcp import audit, server

from .fake_mhphotos import FakeMHPhotos

pytestmark = pytest.mark.anyio
ANNA, BEN, CARA = "17900000000000001", "17900000000000002", "17900000000000003"


@pytest.fixture
def app(monkeypatch):
    f = FakeMHPhotos()
    monkeypatch.setenv("MHPHOTOS_INBOX_URL", f.url)
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "secret")
    monkeypatch.setenv("MHPHOTOS_ALLOW_INSTAGRAM", "1")
    yield f
    f.stop()


def posts(app):
    return [a for a in app.comment_actions if a["action"] in ("reply", "like", "ignore")]


async def test_disabled_without_config():
    assert (await server.mhphotos_comments())["code"] == "disabled"


async def test_list_open_drafts_and_filters(app):
    res = await server.mhphotos_comments()
    assert [c["id"] for c in res["comments"]] == [ANNA]
    assert res["comments"][0]["draft_reply"] == "Am Aasee in Münster!" and res["comments"][0]["liked"] is False
    assert {c["id"] for c in (await server.mhphotos_comments(status="alle"))["comments"]} == {ANNA, BEN}
    assert [c["id"] for c in (await server.mhphotos_comments(status="unliked"))["comments"]] == [ANNA]
    assert [c["id"] for c in (await server.mhphotos_comments("malou", "neu"))["comments"]] == [CARA]
    assert "error" in await server.mhphotos_comments("martin")
    assert "error" in await server.mhphotos_comments(status="kaputt")


async def test_redraft_and_tone(app):
    res = await server.mhphotos_comment_draft(ANNA)
    assert res["draft_reply"] == "Neu (draft)" and res["text"] == "Wo ist das?"
    assert (await server.mhphotos_comment_draft(ANNA, tone="knapp"))["draft_reply"] == "Neu (knapp)"
    assert "tone" in (await server.mhphotos_comment_draft(ANNA, tone="frech"))["error"]
    assert "not found" in (await server.mhphotos_comment_draft("999"))["error"]
    assert "comment_id" in (await server.mhphotos_comment_draft("../x"))["error"]
    assert posts(app) == []


async def test_reply_is_dry_run_until_confirm(app):
    dry = await server.mhphotos_comment_respond(ANNA, reply_text="Am Aasee 🙂", like=True)
    assert dry["dry_run"] and dry["reply_text"] == "Am Aasee 🙂" and dry["like"] and posts(app) == []
    done = await server.mhphotos_comment_respond(ANNA, reply_text="Am Aasee 🙂", like=True, confirm=True)
    assert done["applied"] and done["reply_id"] == "r-1" and done["liked"]
    assert [(a["action"], a.get("text")) for a in posts(app)] == [("reply", "Am Aasee 🙂"), ("like", None)]
    assert audit.read_all()[-1]["tool"] == "mhphotos_comment_respond"


async def test_respond_checks(app, monkeypatch):
    assert "Nothing to do" in (await server.mhphotos_comment_respond(ANNA))["error"]
    assert "not both" in (await server.mhphotos_comment_respond(ANNA, reply_text="x", ignore=True))["error"]
    assert "2200" in (await server.mhphotos_comment_respond(ANNA, reply_text="x" * 2201))["error"]
    warned = await server.mhphotos_comment_respond(BEN, reply_text="Nochmal danke")
    assert any("Already answered" in w for w in warned["warnings"])
    assert (await server.mhphotos_comment_respond(CARA, "malou", ignore=True, confirm=True))["ignored"]
    monkeypatch.delenv("MHPHOTOS_ALLOW_INSTAGRAM")
    assert (await server.mhphotos_comment_respond(ANNA, reply_text="x", confirm=True))["code"] == "disabled"


async def test_declined_approval_posts_nothing(app, monkeypatch):
    async def no(ctx, message):
        return False, "declined"
    monkeypatch.setattr(server.gate, "approve", no)
    res = await server.mhphotos_comment_respond(ANNA, reply_text="Hallo", confirm=True)
    assert "applied" not in res and posts(app) == []


# ---- friends' posts: comment ideas to paste by hand ----------------------------------------
async def test_friend_posts_with_closeness(app):
    res = await server.mhphotos_friend_posts()
    assert [(p["id"], p["username"], p["closeness"]) for p in res["posts"]] == [
        ("wp-1", "tom.berg", "eng"), ("wp-2", "fremd_cafe", "unbekannt")]
    assert res["posts"][0]["friend_note"] == "Schulfreund" and res["ask_martin_about"] == ["fremd_cafe"]
    assert len((await server.mhphotos_friend_posts(status="alle"))["posts"]) == 3
    assert "error" in await server.mhphotos_friend_posts(status="x")


async def test_friend_post_marks_only_the_list(app):
    assert (await server.mhphotos_friend_post("wp-1", "regenerate"))["suggested_comment"] == "Neue Idee"
    await server.mhphotos_friend_post("wp-1", "done")
    assert app.watch_posts[0]["status"] == "kommentiert"
    assert "error" in await server.mhphotos_friend_post("wp-1", "like")
    assert "error" in await server.mhphotos_friend_post("../x", "done")
    assert app.comment_actions == []


async def test_set_friend_creates_and_updates(app):
    assert (await server.mhphotos_set_friend("@Fremd_Cafe", False))["closeness"] == "nicht eng"
    await server.mhphotos_set_friend("tom.berg", False, note="eher Kollege")
    people = {p["username"]: p for p in (await server.mhphotos_friends())["people"]}
    assert people["fremd_cafe"]["closeness"] == "nicht eng"
    assert people["tom.berg"] == {"username": "tom.berg", "closeness": "nicht eng", "note": "eher Kollege"}
    await server.mhphotos_set_friend("tom.berg", True)
    assert [p["username"] for p in (await server.mhphotos_friends(close_only=True))["people"]] == ["tom.berg"]
    assert "error" in await server.mhphotos_set_friend("nicht gültig!", True)
    assert "error" in await server.mhphotos_set_friend("ok", True, note="x" * 201)

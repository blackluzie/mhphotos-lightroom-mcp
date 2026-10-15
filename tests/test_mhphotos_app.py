"""Inbox / analysis / publish / Instagram tools against a fake MHPhotos admin API."""

import json

import pytest
from mhphotos_lightroom_mcp import server

from .fake_mhphotos import FakeMHPhotos

pytestmark = pytest.mark.anyio


@pytest.fixture
def app(monkeypatch):
    f = FakeMHPhotos()
    monkeypatch.setenv("MHPHOTOS_INBOX_URL", f.url)
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "secret")
    yield f
    f.stop()


@pytest.fixture
def can_publish(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_PUBLISH", "1")


async def test_everything_disabled_without_config():
    for res in (
        await server.mhphotos_galleries(),
        await server.mhphotos_inbox(),
        await server.mhphotos_analyze_inbox(["lr-1"]),
        await server.mhphotos_publish(["lr-1"], confirm=True),
        await server.mhphotos_instagram_posts(),
    ):
        assert res["code"] == "disabled"


async def test_wrong_token_is_reported(app, monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "tok-xyz-123")
    res = await server.mhphotos_galleries()
    assert res["code"] == "unauthorized"
    assert "tok-xyz-123" not in json.dumps(res)


async def test_galleries_and_inbox(app):
    gal = await server.mhphotos_galleries()
    assert [g["id"] for g in gal["galleries"]] == ["heimat/muensterland", "unterwegs/staedte"]
    inbox = await server.mhphotos_inbox(status="wartet")
    assert inbox["total"] == 3
    first = next(i for i in inbox["items"] if i["id"] == "lr-1")
    assert first["suggestion"]["title"] == "Abendlicht am Aasee"
    assert first["suggestion"]["gallery_ids"] == ["heimat/muensterland"]
    assert next(i for i in inbox["items"] if i["id"] == "lr-2")["suggestion"] is None


async def test_analyze_queues_and_validates(app):
    res = await server.mhphotos_analyze_inbox(["lr-1", "lr-2"])
    assert [r["status"] for r in res["results"]] == ["queued", "queued"]
    assert ("POST", "/api/admin/lightroom-import/lr-1/analyze", {}) in app.calls
    assert "error" in await server.mhphotos_analyze_inbox([])
    assert "error" in await server.mhphotos_analyze_inbox(["a", "a"])
    assert "error" in await server.mhphotos_analyze_inbox([f"x{i}" for i in range(21)])


async def test_publish_needs_operator_optin(app):
    res = await server.mhphotos_publish(["lr-1"], confirm=True)
    assert res["code"] == "disabled"
    assert not any(c[0] == "PATCH" for c in app.calls)


async def test_publish_dry_run_and_problems(app, can_publish):
    res = await server.mhphotos_publish(["lr-1", "lr-2", "lr-3", "lr-4", "nope"])
    assert res["dry_run"] is True and res["will_be_public"] is True
    assert res["ready"] == 1
    by_id = {e["id"]: e for e in res["items"]}
    assert by_id["lr-1"]["title"] == "Abendlicht am Aasee"
    assert by_id["lr-1"]["categories"] == ["heimat/muensterland"]
    assert "analyze" in by_id["lr-2"]["problem"]
    assert by_id["lr-3"]["problem"] == "already published"
    assert "unknown category" in by_id["lr-4"]["problem"]
    assert by_id["nope"]["problem"] == "not found in inbox"
    assert not any(c[0] == "PATCH" for c in app.calls)  # a dry run publishes nothing


async def test_publish_confirmed_uses_suggestion_and_overrides(app, can_publish):
    res = await server.mhphotos_publish(
        ["lr-1", "lr-2"],
        overrides={"lr-2": {"title": "Eigener Titel", "categories": ["unterwegs/staedte"], "description": "Text"}},
        confirm=True,
    )
    assert res["published"] == 2
    patches = {c[1].rsplit("/", 1)[1]: c[2] for c in app.calls if c[0] == "PATCH"}
    assert patches["lr-1"]["action"] == "approve"
    assert patches["lr-1"]["title"] == "Abendlicht am Aasee"
    assert patches["lr-1"]["categories"] == ["heimat/muensterland"]
    assert patches["lr-2"]["title"] == "Eigener Titel"
    assert patches["lr-2"]["categories"] == ["unterwegs/staedte"]
    assert {e["id"]: e["status"] for e in res["items"]} == {"lr-1": "published", "lr-2": "published"}
    again = await server.mhphotos_publish(["lr-1"], confirm=True)  # already public -> skipped
    assert again["published"] == 0 and again["items"][0]["problem"] == "already published"


async def test_publish_skips_invalid_but_publishes_valid(app, can_publish):
    res = await server.mhphotos_publish(["lr-1", "lr-4"], confirm=True)
    assert res["published"] == 1
    assert sum(1 for c in app.calls if c[0] == "PATCH") == 1


async def test_publish_override_validation(app, can_publish):
    assert "error" in await server.mhphotos_publish(["lr-1"], overrides={"lr-9": {"title": "x"}})
    assert "error" in await server.mhphotos_publish(["lr-1"], overrides={"lr-1": {"title": "x" * 121}})
    assert "error" in await server.mhphotos_publish(["lr-1"], overrides={"lr-1": {"categories": "heimat"}})


async def test_instagram_posts(app):
    res = await server.mhphotos_instagram_posts(year=2026)
    assert [p["id"] for p in res["posts"]] == ["ig1"]  # ig2 is already on the homepage
    assert res["newest_sync"] == "2026-10-01T00:00:00Z"
    both = await server.mhphotos_instagram_posts(year=2026, only_not_on_homepage=False)
    assert [p["id"] for p in both["posts"]] == ["ig2", "ig1"]
    assert (await server.mhphotos_instagram_posts())["total"] == 2

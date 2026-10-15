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


# ---- Instagram images -------------------------------------------------------
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from mcp.server.fastmcp import Image
from mhphotos_lightroom_mcp import publishing


class _Cdn:
    def __init__(self):
        outer = self
        self.hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                if self.path == "/ok.jpg":
                    body, ctype, status = b"\xff\xd8\xff\xd9", "image/jpeg", 200
                elif self.path == "/gone.jpg":
                    body, ctype, status = b"", "text/plain", 403
                elif self.path == "/html":
                    body, ctype, status = b"<html>", "text/html", 200
                elif self.path == "/big.jpg":
                    body, ctype, status = b"x" * (publishing.MAX_IMAGE_BYTES + 10), "image/jpeg", 200
                elif self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "http://evil.example/steal.jpg")
                    self.end_headers()
                    return
                else:
                    body, ctype, status = b"", "text/plain", 404
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def cdn(monkeypatch):
    c = _Cdn()
    monkeypatch.setattr(publishing, "IMAGE_HOST_SUFFIXES", ("127.0.0.1",))  # test double for the CDN
    monkeypatch.setattr(publishing, "IMAGE_SCHEMES", ("http", "https"))
    yield c
    c.httpd.shutdown()
    c.httpd.server_close()


def test_image_url_allowlist():
    ok = ["https://scontent-fra5-1.cdninstagram.com/v/a.jpg", "https://scontent.xx.fbcdn.net/a.jpg",
          "https://www.instagram.com/p/x/media"]
    bad = ["http://scontent.cdninstagram.com/a.jpg", "https://evil.example/a.jpg",
           "https://cdninstagram.com.evil.example/a.jpg", "https://evilcdninstagram.com/a.jpg",
           "file:///etc/passwd", "ftp://x.cdninstagram.com/a", ""]
    assert all(publishing.image_url_allowed(u) for u in ok)
    assert not any(publishing.image_url_allowed(u) for u in bad)


async def test_instagram_images_fetch_only_allowed_hosts(app):
    app.instagram = [{"id": "ig1", "mediaType": "IMAGE", "mediaUrl": "https://evil.example/a.jpg",
                      "timestamp": "2026-03-01T08:00:00+0000"}]
    res = await server.mhphotos_instagram_images(["ig1"])
    assert "not an Instagram" in res[0]["error"]


async def test_instagram_images_roundtrip(app, cdn):
    app.instagram = [
        {"id": "ig1", "mediaType": "IMAGE", "mediaUrl": cdn.base + "/ok.jpg", "caption": "Aasee",
         "timestamp": "2026-03-01T08:00:00+0000", "permalink": "https://instagram.com/p/1"},
        {"id": "ig2", "mediaType": "VIDEO", "thumbnailUrl": cdn.base + "/ok.jpg", "mediaUrl": cdn.base + "/video.mp4"},
        {"id": "ig3", "mediaType": "IMAGE", "mediaUrl": cdn.base + "/gone.jpg"},
        {"id": "ig4", "mediaType": "IMAGE", "mediaUrl": cdn.base + "/html"},
        {"id": "ig5", "mediaType": "IMAGE", "mediaUrl": cdn.base + "/big.jpg"},
        {"id": "ig6", "mediaType": "IMAGE", "mediaUrl": cdn.base + "/redir"},
    ]
    res = await server.mhphotos_instagram_images(["ig1", "ig2", "ig3", "ig4", "ig5", "ig6"])
    assert res[0]["id"] == "ig1" and res[0]["caption"] == "Aasee"
    assert isinstance(res[1], Image) and res[1].data == b"\xff\xd8\xff\xd9"
    assert res[2]["id"] == "ig2" and isinstance(res[3], Image)
    assert "expired" in res[4]["error"]
    assert "not an image" in res[5]["error"]
    assert "larger than 6 MB" in res[6]["error"]
    assert "download failed" in res[7]["error"]  # redirect to evil.example was refused
    assert "/video.mp4" not in cdn.hits  # videos use their thumbnail


async def test_instagram_images_validation(app):
    assert "error" in (await server.mhphotos_instagram_images([]))[0]
    assert "error" in (await server.mhphotos_instagram_images([f"i{n}" for n in range(7)]))[0]
    assert "not found" in (await server.mhphotos_instagram_images(["nope"]))[0]["error"]


async def test_instagram_images_disabled_without_config():
    assert (await server.mhphotos_instagram_images(["ig1"]))[0]["code"] == "disabled"

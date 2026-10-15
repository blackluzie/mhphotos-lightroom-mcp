"""send_to_mhphotos_inbox against a local fake inbox that mimics the MHPhotos endpoint."""

import json
import threading
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from mhphotos_lightroom_mcp import publishing, server

pytestmark = pytest.mark.anyio


class _Inbox:
    def __init__(self):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                msg = BytesParser().parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body
                )
                fields = {}
                for part in msg.get_payload():
                    name = part.get_param("name", header="content-disposition")
                    fields[name] = part.get_payload(decode=True)
                outer.requests.append(
                    {"path": self.path, "token": self.headers.get("X-Admin-Token"), "fields": fields}
                )
                if self.headers.get("X-Admin-Token") != "secret":
                    self.send_response(401)
                    self.end_headers()
                    return
                dup = len(outer.requests) == 2 and outer.dup_second
                raw = json.dumps({"ok": True, "duplicate": dup, "item": {"id": f"lr-{len(outer.requests)}"}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.dup_second = False
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def inbox():
    i = _Inbox()
    yield i
    i.httpd.shutdown()
    i.httpd.server_close()


@pytest.fixture
def configured(inbox, monkeypatch, tmp_path):
    monkeypatch.setenv("MHPHOTOS_INBOX_URL", inbox.url)
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "secret")
    monkeypatch.setenv("MHPHOTOS_EXPORT_DIR", str(tmp_path / "exp"))
    return inbox


def test_config_validation(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "t")
    for bad in ("", "http://example.com", "ftp://x", "not a url"):
        monkeypatch.setenv("MHPHOTOS_INBOX_URL", bad)
        assert publishing.inbox_config() is None
    for good in ("https://mhphotos.example", "http://127.0.0.1:8787"):
        monkeypatch.setenv("MHPHOTOS_INBOX_URL", good)
        assert publishing.inbox_config() is not None


async def test_disabled_by_default():
    res = await server.send_to_mhphotos_inbox(["p1"], confirm=True)
    assert res["code"] == "disabled"


async def test_dry_run_sends_nothing(configured, tmp_path):
    res = await server.send_to_mhphotos_inbox(["p1", "p2"])
    assert res["dry_run"] is True
    assert res["files"] == ["IMG_0001.dng", "IMG_0002.dng"]
    assert configured.requests == []
    assert not (tmp_path / "exp").exists()


async def test_confirmed_upload(configured):
    configured.dup_second = True
    res = await server.send_to_mhphotos_inbox(["p1", "p2"], confirm=True)
    assert res["applied"] is True
    assert [r["status"] for r in res["results"]] == ["uploaded", "duplicate"]
    first = configured.requests[0]
    assert first["path"] == "/api/admin/lightroom-import"
    assert first["token"] == "secret"
    assert first["fields"]["source"] == b"lightroom-classic-plugin"
    assert first["fields"]["rating"] == b"5"
    assert first["fields"]["keywords"] == b"portrait,studio"
    assert first["fields"]["file"][:2] == b"\xff\xd8"  # a JPEG


async def test_wrong_token_reports_error_without_leaking(configured, monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "tok-xyz-123")
    res = await server.send_to_mhphotos_inbox(["p1"], confirm=True)
    assert res["results"][0]["status"] == "error"
    assert "401" in res["results"][0]["error"]
    assert "tok-xyz-123" not in json.dumps(res)


async def test_unknown_photo_sends_nothing(configured):
    res = await server.send_to_mhphotos_inbox(["p1", "nope"], confirm=True)
    assert "error" in res
    assert configured.requests == []

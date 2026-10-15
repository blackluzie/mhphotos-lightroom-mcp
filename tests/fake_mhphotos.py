"""In-memory stand-in for the MHPhotos admin API (routes copied from apps-platform/apps/mhphotos)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = "secret"


class FakeMHPhotos:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.galleries = [
            {"id": "heimat/muensterland", "title": "Münsterland", "area": "heimat"},
            {"id": "unterwegs/staedte", "title": "Städte", "area": "unterwegs"},
        ]
        self.items = {
            "lr-1": {
                "id": "lr-1", "originalFileName": "IMG_0001.jpg", "status": "wartet", "rating": 5,
                "captureDate": "2026-05-01T10:15:00.000Z",
                "aiSuggestion": {
                    "title": "Abendlicht am Aasee", "description": "Warmes Licht über dem Wasser.",
                    "alt": "Sonnenuntergang am See", "tags": ["see"], "galleryIds": ["heimat/muensterland"],
                    "galleryTitles": ["Münsterland"], "confidence": 0.9,
                },
            },
            "lr-2": {"id": "lr-2", "originalFileName": "IMG_0002.jpg", "status": "wartet", "rating": 5},
            "lr-3": {"id": "lr-3", "originalFileName": "IMG_0003.jpg", "status": "uebernommen", "rating": 5,
                     "aiSuggestion": {"title": "x", "galleryIds": ["unterwegs/staedte"]}},
            "lr-4": {"id": "lr-4", "originalFileName": "IMG_0004.jpg", "status": "wartet", "rating": 5,
                     "aiSuggestion": {"title": "Kirche", "description": "d", "galleryIds": ["gibt/es/nicht"]}},
        }
        self.instagram = [
            {"id": "ig1", "timestamp": "2026-03-01T08:00:00+0000", "mediaType": "IMAGE", "caption": "Hallo",
             "permalink": "https://instagram.com/p/1", "thumbnailUrl": "https://cdn/1.jpg", "syncedAt": "2026-10-01T00:00:00Z"},
            {"id": "ig2", "timestamp": "2026-04-01T08:00:00+0000", "mediaType": "IMAGE", "homepagePhotoId": "foto-1",
             "permalink": "https://instagram.com/p/2", "syncedAt": "2026-10-01T00:00:00Z"},
            {"id": "ig3", "timestamp": "2025-04-01T08:00:00+0000", "mediaType": "IMAGE", "syncedAt": "2026-10-01T00:00:00Z"},
        ]
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                outer.calls.append((method, self.path, body))
                if self.headers.get("X-Admin-Token") != TOKEN:
                    return self._reply(401, {"error": "unauthorized"})
                path = self.path
                if method == "GET" and path == "/api/admin/galleries":
                    return self._reply(200, {"galleries": outer.galleries})
                if method == "GET" and path == "/api/admin/lightroom-import":
                    return self._reply(200, {"items": list(outer.items.values())})
                if method == "GET" and path == "/api/admin/instagram-library":
                    return self._reply(200, {"items": outer.instagram, "photos": []})
                if method == "POST" and path.endswith("/analyze"):
                    return self._reply(202, {"ok": True, "status": "queued"})
                if method == "PATCH" and path.startswith("/api/admin/lightroom-import/"):
                    item = outer.items[path.rsplit("/", 1)[1]]
                    item["status"] = "uebernommen"
                    return self._reply(200, {"ok": True, "item": item, "photo": {"id": f"foto-{item['id']}"}})
                self._reply(404, {"error": "not found"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PATCH(self):
                self._handle("PATCH")

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

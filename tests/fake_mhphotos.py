"""In-memory stand-in for the MHPhotos admin API (routes copied from apps-platform/apps/mhphotos)."""

import json
import threading
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = "secret"


def _jpeg(colour):
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (1200, 800), colour).save(out, "JPEG")
    return out.getvalue()


class FakeMHPhotos:
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.social_queue: list[dict] = [
            {"id": "q-old", "account_id": "mhphotos", "account_name": "MHPhotos", "type": "post",
             "status": "veroeffentlicht", "caption": "alt", "scheduled_at": "2026-01-01T10:00:00.000Z",
             "permalink": "https://instagram.com/p/old"},
        ]
        self.social_uploads: list[dict] = []
        self.approval = True  # False = an MHPhotos version without "wartet auf Freigabe"
        self.comments: list[dict] = [
            {"id": "17900000000000001", "account_id": "mhphotos", "username": "anna.k", "text": "Wo ist das?",
             "media_caption": "Abendlicht am Aasee", "media_id": "m1", "classification": "frage",
             "status": "entwurf", "draft_reply": "Am Aasee in Münster!", "reply_text": "", "liked_at": None},
            {"id": "17900000000000002", "account_id": "mhphotos", "username": "ben", "text": "Wow 😍",
             "media_caption": "Hafen", "media_id": "m2", "classification": "lob", "status": "beantwortet",
             "draft_reply": "", "reply_text": "Danke!", "liked_at": "2026-11-01 10:00:00"},
            {"id": "17900000000000003", "account_id": "malou", "username": "cara", "text": "Schön",
             "media_caption": "", "media_id": "m3", "classification": "lob", "status": "neu",
             "draft_reply": "", "reply_text": "", "liked_at": None},
        ]
        self.comment_actions: list[dict] = []
        self.watch_posts: list[dict] = [
            {"id": "wp-1", "ig_username": "Tom.Berg", "caption": "Gipfel!", "permalink": "https://instagram.com/p/t1",
             "posted_at": "2026-11-07", "status": "neu", "suggested_comment": "Starker Blick!"},
            {"id": "wp-2", "ig_username": "fremd_cafe", "caption": "Neue Torte", "permalink": "https://instagram.com/p/f1",
             "posted_at": "2026-11-06", "status": "neu", "suggested_comment": ""},
            {"id": "wp-3", "ig_username": "tom.berg", "caption": "alt", "status": "kommentiert", "suggested_comment": "x"},
        ]
        self.contacts: list[dict] = [{"id": "pc-1", "username": "tom.berg", "note": "Schulfreund", "close_friend": 1}]
        self.duplicate: dict | None = None  # set to simulate "already planned"
        self.duplicate_after: int | None = None
        self.reel_jobs: list[dict] = []
        self.site_photos = [
            {"id": "hp-1", "title": "Dom im Nebel", "location": "Münster", "year": 2025, "categories": ["heimat/muensterland"],
             "uploadedAt": "2026-01-02"},
            {"id": "hp-2", "title": "Hafen", "location": "Palma", "year": 2026, "categories": ["unterwegs/staedte"],
             "uploadedAt": "2026-03-01"},
        ]  # the first n checks are new, the rest "already posted"
        self.images = {k: _jpeg(c) for k, c in (("lr-1", (200, 80, 40)), ("lr-2", (40, 120, 200)),
                                                  ("lr-3", (60, 160, 60)), ("lr-4", (180, 180, 40)))}
        self.galleries = [
            {"id": "heimat/muensterland", "title": "Münsterland", "area": "heimat"},
            {"id": "unterwegs/staedte", "title": "Städte", "area": "unterwegs"},
        ]
        self.items = {
            "lr-1": {
                "id": "lr-1", "originalFileName": "IMG_0001.jpg", "status": "wartet", "rating": 5,
                "source": "lightroom-mobile", "camera": "iPhone 15 Pro", "location": "Palma",
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
        self.gear = [
            # shape of apps/mhphotos lib/gear.ts: German category, optional sharedCategory/brand/model
            {"id": "g-r5", "name": "Canon EOS R5", "category": "kamera", "sharedCategory": "camera"},
            {"id": "g-2470", "name": "RF 24-70mm F2.8", "category": "objektiv", "brand": "Canon", "model": "24-70mm F2.8",
             "status": "active"},
            {"id": "g-old", "name": "Fuji X100", "category": "kamera", "brand": "Fujifilm", "model": "X100"},
            {"id": "g-tripod", "name": "Gitzo", "category": "stativ"},
        ]
        self.spots = [
            {"id": "s-seiser", "name": "Seiser Alm", "lat": 46.5401, "lon": 11.6201},
            {"id": "s-dom", "name": "Dom Münster", "lat": 51.9625, "lon": 7.6256},
            {"id": "s-none", "name": "Ohne Position"},
        ]
        self.gear_usage: dict | None = None
        self.spot_photos: dict | None = None
        self.blog_posts: list[dict] = []
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
                raw = self.rfile.read(n) if n else b""
                ctype = self.headers.get("Content-Type", "")
                if ctype.startswith("multipart/"):
                    msg = BytesParser().parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
                    body = {}
                    for part in msg.get_payload():
                        name = part.get_param("name", header="content-disposition")
                        if name == "extra":  # repeated: further carousel images
                            body.setdefault("extra", []).append(part.get_payload(decode=True))
                        else:
                            body[name] = part.get_payload(decode=True)
                else:
                    body = json.loads(raw or b"{}") if raw else {}
                outer.calls.append((method, self.path, body))
                if self.headers.get("User-Agent", "").startswith("Python-urllib"):
                    # Cloudflare in front of mhphotos.de blocks the stock urllib agent.
                    return self._reply(403, {"error": "error code: 1010"})
                if self.headers.get("X-Admin-Token") != TOKEN:
                    return self._reply(401, {"error": "unauthorized"})
                path = self.path
                if method == "GET" and path.startswith("/api/admin/social-hub-comments?"):
                    from urllib.parse import parse_qs, urlsplit
                    q = {k: v[0] for k, v in parse_qs(urlsplit(path).query, keep_blank_values=True).items()}
                    if q.get("accountId") not in ("mhphotos", "malou"):
                        return self._reply(400, {"error": "invalid_account"})
                    rows = [c for c in outer.comments if c["account_id"] == q["accountId"]]
                    wanted = q.get("filter", "entwurf")
                    if wanted == "unliked":
                        rows = [c for c in rows if not c["liked_at"] and c["status"] != "ignoriert"]
                    elif wanted:
                        rows = [c for c in rows if c["status"] == wanted]
                    return self._reply(200, rows)
                if method == "POST" and path == "/api/admin/social-hub-comments":
                    outer.comment_actions.append(body)
                    row = next((c for c in outer.comments if c["id"] == body.get("id")), None)
                    if row is None:
                        return self._reply(502, {"error": "Kommentar nicht gefunden."})
                    action = body.get("action")
                    if action == "reply":
                        row.update(status="beantwortet", reply_text=body["text"])
                        return self._reply(200, {"ok": True, "replyId": "r-1"})
                    if action == "like":
                        row["liked_at"] = "now"
                        return self._reply(200, {"ok": True, "liked": True})
                    if action == "ignore":
                        row["status"] = "ignoriert"
                        return self._reply(200, {"ok": True})
                    if action in ("draft", "tone"):
                        row.update(status="entwurf", draft_reply=f"Neu ({body.get('tone', 'draft')})")
                        return self._reply(200, {"ok": True, "draft_reply": row["draft_reply"], "classification": "frage"})
                    return self._reply(400, {"error": "invalid_action"})
                if path.startswith("/api/admin/social-hub/watchlist/posts"):
                    from urllib.parse import parse_qs, urlsplit
                    parts = urlsplit(path)
                    if method == "GET":
                        wanted = parse_qs(parts.query).get("status", [""])[0]
                        return self._reply(200, [p for p in outer.watch_posts if not wanted or p["status"] == wanted])
                    pid, action = parts.path.split("/")[-2:]
                    post = next((p for p in outer.watch_posts if p["id"] == pid), None)
                    if post is None:
                        return self._reply(404, {"error": "Eintrag nicht gefunden."})
                    if action == "regenerate":
                        post["suggested_comment"] = "Neue Idee"
                        return self._reply(200, {"ok": True, "suggested_comment": "Neue Idee"})
                    post["status"] = {"done": "kommentiert", "ignore": "ignoriert"}[action]
                    return self._reply(200, {"ok": True})
                if path == "/api/admin/social-hub/contacts":
                    if method == "GET":
                        return self._reply(200, outer.contacts)
                    if not any(c["username"] == body["username"] for c in outer.contacts):
                        outer.contacts.append({"id": f"pc-{len(outer.contacts) + 1}", "username": body["username"],
                                               "note": body.get("note", ""), "close_friend": 0 if body.get("close_friend") is False else 1})
                    return self._reply(200, {"ok": True})
                if method == "PATCH" and path.startswith("/api/admin/social-hub/contacts/"):
                    row = next(c for c in outer.contacts if c["id"] == path.rsplit("/", 1)[1])
                    if "close_friend" in body:
                        row["close_friend"] = 1 if body["close_friend"] else 0
                    if "note" in body:
                        row["note"] = body["note"]
                    return self._reply(200, {"ok": True})
                if path == "/api/admin/social-hub-queue":
                    if method == "GET":
                        return self._reply(200, outer.social_queue)
                    if method == "POST":
                        outer.social_uploads.append(body)
                        accounts = body["accountId"].decode().split(",")
                        status = "freigabe" if body.get("needsApproval") == b"true" and outer.approval else "geplant"
                        ids = []
                        for acc in accounts:
                            rid = f"q-{len(outer.social_queue):04d}"
                            ids.append(rid)
                            outer.social_queue.append({
                                "id": rid, "account_id": acc, "account_name": acc.title(),
                                "type": body["type"].decode(), "caption": body["caption"].decode(),
                                "scheduled_at": body["scheduledAt"].decode(), "status": status,
                            })
                        return self._reply(200, {"ok": True, "id": ids[0], **({"ids": ids} if len(ids) > 1 else {}),
                                                 **({"status": status} if outer.approval else {})})
                if method == "GET" and path == "/api/admin/photos":
                    return self._reply(200, {"photos": outer.site_photos})
                if path == "/api/admin/reel-jobs":
                    if method == "GET":
                        return self._reply(200, {"jobs": outer.reel_jobs})
                    if len(body.get("photoIds") or []) < 3:
                        return self._reply(400, {"error": "Mindestens drei Bilder auswählen."})
                    job = {"id": f"reel-{len(outer.reel_jobs):04d}", "status": "wartet", **body}
                    outer.reel_jobs.insert(0, job)
                    return self._reply(200, {"ok": True, "job": job, "recovered": False})
                if method == "GET" and path.startswith("/api/admin/social-hub-best-time?accountId="):
                    return self._reply(200, {"slots": [{"weekday": 3, "hour": 19}], "account": path.rsplit("=", 1)[1]})
                if method == "POST" and path == "/api/admin/social-hub-duplicate":
                    if outer.duplicate_after is not None:
                        outer.duplicate_after -= 1
                        if outer.duplicate_after < 0:
                            return self._reply(200, {"duplicate": True, "hash": "0123456789abcdef"})
                    if outer.duplicate:
                        return self._reply(200, {"duplicate": True, "hash": "0123456789abcdef", **outer.duplicate})
                    return self._reply(200, {"duplicate": False, "hash": "0123456789abcdef"})
                if method == "DELETE" and path.startswith("/api/admin/social-hub/queue/"):
                    wanted = path.rsplit("/", 1)[1]
                    before = len(outer.social_queue)
                    outer.social_queue = [r for r in outer.social_queue if r["id"] != wanted]
                    if len(outer.social_queue) == before:
                        return self._reply(404, {"error": "Eintrag nicht gefunden"})
                    return self._reply(200, {"ok": True})
                if method == "GET" and path.startswith("/api/admin/lightroom-import/") and path.endswith("/image"):
                    data = outer.images.get(path.split("/")[-2])
                    if data is None:
                        return self._reply(404, {"error": "nicht gefunden"})
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if path == "/api/admin/gear" and method == "GET":
                    return self._reply(200, {"gear": outer.gear})
                if path == "/api/admin/gear/usage" and method == "PUT":
                    outer.gear_usage = body
                    return self._reply(200, {"ok": True})
                if path == "/api/admin/spots" and method == "GET":
                    return self._reply(200, {"spots": outer.spots})
                if path == "/api/admin/spots/lightroom-photos" and method == "PUT":
                    outer.spot_photos = body
                    return self._reply(200, {"ok": True})
                if path == "/api/admin/blog" and method == "POST":
                    post = {"id": f"post-{len(outer.blog_posts) + 1}", "slug": "reise", **body}
                    outer.blog_posts.append(post)
                    return self._reply(201, {"post": post})
                if method == "GET" and path == "/api/admin/galleries":
                    return self._reply(200, {"galleries": outer.galleries})
                if method == "GET" and path == "/api/admin/lightroom-import":
                    return self._reply(200, {"items": list(outer.items.values())})
                if method == "GET" and path == "/api/admin/instagram-library":
                    return self._reply(200, {"items": outer.instagram, "photos": []})
                if method == "POST" and path.endswith("/analyze"):
                    return self._reply(202, {"ok": True, "status": "queued"})
                if method == "PATCH" and path.startswith("/api/admin/photos/"):
                    photo = next((p for p in outer.site_photos if p["id"] == path.rsplit("/", 1)[1]), None)
                    if photo is None:
                        return self._reply(404, {"error": "nicht gefunden"})
                    photo.update(body)
                    return self._reply(200, {"ok": True, "photo": photo})
                if method == "POST" and path == "/api/admin/instagram-library":
                    if body.get("action") == "link":
                        return self._reply(200, {"ok": True, "item": {"id": body["instagramId"],
                                                                      "homepagePhotoId": body["photoId"]}})
                    return self._reply(400, {"error": "Ungültige Aktion"})
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

            def do_DELETE(self):
                self._handle("DELETE")

            def do_PUT(self):
                self._handle("PUT")

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

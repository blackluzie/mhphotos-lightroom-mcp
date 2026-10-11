"""Opt-in localhost bridge with bounded, in-memory selection metadata.

No direct catalog access. Only an authenticated local client can submit data.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 8192
MAX_PHOTOS = 5
SELECTION_TTL_SECONDS = 300
FIELDS = {"fileName", "rating", "pickStatus"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "MHPhotosBridge/0.2"

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, f"Bearer {self.server.bridge_token}")

    def do_GET(self) -> None:
        if not self._authorized():
            return self._respond(401, {"error": "unauthorized"})
        if self.path == "/v1/status":
            with self.server.state_lock:
                has_selection = self.server.selection is not None and (\n                    time.monotonic() - self.server.selection_updated_at\n                    < SELECTION_TTL_SECONDS\n                )
            return self._respond(200, {
                "connected": False,
                "bridge_running": True,
                "lightroom_connected": False,
                "selection_received": has_selection,
                "mode": "prototype",
            })
        if self.path == "/v1/selection":
            with self.server.state_lock:
                selection = self.server.selection\n                if selection is not None and (\n                    time.monotonic() - self.server.selection_updated_at\n                    >= SELECTION_TTL_SECONDS\n                ):\n                    self.server.selection = None\n                    selection = None\n            if selection is None:
                return self._respond(404, {"error": "no_selection"})
            return self._respond(200, {"photos": selection})
        self._respond(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._authorized():
            return self._respond(401, {"error": "unauthorized"})
        if self.path != "/v1/selection":
            return self._respond(404, {"error": "not_found"})
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            return self._respond(400, {"error": "invalid_length"})
        if not 0 <= length <= MAX_BODY:
            return self._respond(413, {"error": "body_too_large"})
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return self._respond(400, {"error": "invalid_json"})
        if not isinstance(payload, dict) or set(payload) != {"photos"}:
            return self._respond(400, {"error": "invalid_payload"})
        photos = payload["photos"]
        if not isinstance(photos, list) or not 1 <= len(photos) <= MAX_PHOTOS:
            return self._respond(400, {"error": "invalid_photos"})
        for photo in photos:
            if not isinstance(photo, dict) or not set(photo).issubset(FIELDS):
                return self._respond(400, {"error": "invalid_fields"})
            if any(
                not isinstance(value, (str, int)) or isinstance(value, bool)
                or len(str(value)) > 80
                for value in photo.values()
            ):
                return self._respond(400, {"error": "invalid_values"})
        with self.server.state_lock:
            self.server.selection = photos\n            self.server.selection_updated_at = time.monotonic()
        self._respond(200, {"accepted": len(photos)})

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def make_server(token: str, port: int = 8765) -> ThreadingHTTPServer:
    """Create an authenticated listener bound strictly to IPv4 loopback."""
    if len(token) < 32:
        raise ValueError("Bridge token must contain at least 32 characters")
    if not 0 <= port <= 65535:
        raise ValueError("Invalid port")
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.bridge_token = token
    server.selection = None\n    server.selection_updated_at = 0.0
    server.state_lock = threading.Lock()
    server.daemon_threads = True
    return server


def main() -> None:
    token = os.environ.get("MHPHOTOS_BRIDGE_TOKEN", "")
    server = make_server(token)
    print(f"MHPhotos prototype bridge listening on 127.0.0.1:{server.server_port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

"""Opt-in localhost status endpoint for the Lightroom bridge prototype.

This service reports *Python bridge* readiness only. It does not imply that
Lightroom Classic is connected or that a catalog is accessible.
"""
from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    server_version = "MHPhotosBridge/0.1"

    def do_GET(self) -> None:
        expected = self.server.bridge_token
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {expected}"):
            self._respond(401, {"error": "unauthorized"})
        elif self.path != "/v1/status":
            self._respond(404, {"error": "not_found"})
        else:
            self._respond(200, {
                "connected": False,
                "bridge_running": True,
                "lightroom_connected": False,
                "mode": "prototype",
            })

    def do_POST(self) -> None:
        self._respond(405, {"error": "method_not_allowed"})

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Do not log headers or authentication credentials.
        pass


def make_server(token: str, port: int = 8765) -> ThreadingHTTPServer:
    """Create an authenticated listener bound strictly to IPv4 loopback."""
    if len(token) < 32:
        raise ValueError("Bridge token must contain at least 32 characters")
    if not 0 <= port <= 65535:
        raise ValueError("Invalid port")
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.bridge_token = token
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

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

from mhphotos_lightroom_mcp.collection_schema import (
    MAX_COLLECTION_BODY,
    MAX_COLLECTIONS,
    validate_collections,
)
from mhphotos_lightroom_mcp.live_reads import MAX_LIVE_BODY, LiveReads
from mhphotos_lightroom_mcp.selection_schema import (
    FIELDS,
    MAX_BODY,
    MAX_PHOTOS,
    PROTOCOL_VERSION,
    validate_selection,
)

SELECTION_TTL_SECONDS = 300


class _Handler(BaseHTTPRequestHandler):
    server_version = "MHPhotosBridge/0.3"

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, f"Bearer {self.server.bridge_token}")

    def _local_origin(self) -> bool:
        """Reject browser cross-origin requests, even with a leaked token."""
        origin = self.headers.get("Origin")
        port = self.server.server_port
        return origin is None or origin in {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }

    def do_GET(self) -> None:
        if not self._authorized():
            return self._respond(401, {"error": "unauthorized"})
        if self.path == "/v1/capabilities":
            return self._respond(
                200,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "metadata_fields": sorted(FIELDS),
                    "max_photos": MAX_PHOTOS,
                    "max_body_bytes": MAX_BODY,
                    "catalog_writes": False,
                    "collection_overview": True,
                    "max_collections": MAX_COLLECTIONS,
                    "live_reads": self.server.live_reads.enabled,
                },
            )
        if self.path == "/v1/status":
            with self.server.state_lock:
                if self.server.selection is not None and (
                    time.monotonic() - self.server.selection_updated_at
                    >= SELECTION_TTL_SECONDS
                ):
                    self.server.selection = None
                has_selection = self.server.selection is not None
                age = (
                    int(max(0, time.monotonic() - self.server.selection_updated_at))
                    if self.server.selection_updated_at
                    else None
                )
                count = len(self.server.selection) if has_selection else 0
                if self.server.collections is not None and (
                    time.monotonic() - self.server.collections_updated_at
                    >= SELECTION_TTL_SECONDS
                ):
                    self.server.collections = None
                has_collections = self.server.collections is not None
                collection_age = (
                    int(max(0, time.monotonic() - self.server.collections_updated_at))
                    if self.server.collections_updated_at
                    else None
                )
                collection_state = (
                    "received"
                    if has_collections
                    else ("expired" if collection_age is not None else "none")
                )
                collection_count = (
                    len(self.server.collections["collections"])
                    if has_collections
                    else 0
                )
                collection_truncated = (
                    self.server.collections["truncated"] if has_collections else False
                )
                state = (
                    "received"
                    if has_selection
                    else ("expired" if age is not None else "none")
                )
            return self._respond(
                200,
                {
                    "connected": False,
                    "bridge_running": True,
                    "lightroom_connected": False,
                    "selection_received": has_selection,
                    "selection_state": state,
                    "collection_state": collection_state,
                    "collection_count": collection_count,
                    "collection_age_seconds": collection_age,
                    "collection_expires_in_seconds": max(
                        0, SELECTION_TTL_SECONDS - collection_age
                    )
                    if collection_age is not None
                    else 0,
                    "collection_overview_truncated": collection_truncated,
                    "selection_count": count,
                    "selection_age_seconds": age,
                    "selection_expires_in_seconds": max(0, SELECTION_TTL_SECONDS - age)
                    if age is not None
                    else 0,
                    "protocol_version": PROTOCOL_VERSION,
                    "catalog_writes": False,
                    "mode": "prototype",
                    "live_reads_enabled": self.server.live_reads.enabled,
                    "lightroom_worker_online": self.server.live_reads.online(),
                },
            )
        if self.path == "/v1/collections":
            with self.server.state_lock:
                overview = self.server.collections
                if overview is not None and (
                    time.monotonic() - self.server.collections_updated_at
                    >= SELECTION_TTL_SECONDS
                ):
                    self.server.collections = None
                    overview = None
            if overview is None:
                return self._respond(404, {"error": "no_collections"})
            return self._respond(200, overview)
        if self.path == "/v1/selection":
            with self.server.state_lock:
                selection = self.server.selection
                if selection is not None and (
                    time.monotonic() - self.server.selection_updated_at
                    >= SELECTION_TTL_SECONDS
                ):
                    self.server.selection = None
                    selection = None
            if selection is None:
                return self._respond(404, {"error": "no_selection"})
            return self._respond(200, {"photos": selection})
        self._respond(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._local_origin():
            return self._respond(403, {"error": "forbidden_origin"})
        if not self._authorized():
            return self._respond(401, {"error": "unauthorized"})
        if self.path.startswith("/v1/live/"):
            return self._live_post()
        if self.path not in {"/v1/selection", "/v1/collections"}:
            return self._respond(404, {"error": "not_found"})
        if self.headers.get_content_type() != "application/json":
            return self._respond(415, {"error": "unsupported_media_type"})
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            return self._respond(400, {"error": "invalid_length"})
        limit = MAX_COLLECTION_BODY if self.path == "/v1/collections" else MAX_BODY
        if not 0 <= length <= limit:
            return self._respond(413, {"error": "body_too_large"})
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return self._respond(400, {"error": "invalid_json"})
        if self.path == "/v1/collections":
            try:
                collections = validate_collections(payload)
            except ValueError as error:
                return self._respond(400, {"error": str(error)})
            with self.server.state_lock:
                self.server.collections = payload
                self.server.collections_updated_at = time.monotonic()
            return self._respond(200, {"accepted": len(collections)})
        try:
            photos = validate_selection(payload)
        except ValueError as error:
            return self._respond(400, {"error": str(error)})
        with self.server.state_lock:
            self.server.selection = photos
            self.server.selection_updated_at = time.monotonic()
        self._respond(200, {"accepted": len(photos)})

    def _live_post(self):
        if self.path not in {"/v1/live/read", "/v1/live/poll", "/v1/live/reply", "/v1/live/stop"}:
            return self._respond(404, {"error": "not_found"})
        if not self.server.live_reads.enabled:
            return self._respond(403, {"error": "live_reads_disabled"})
        if self.headers.get_content_type() != "application/json":
            return self._respond(415, {"error": "unsupported_media_type"})
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            limit = MAX_LIVE_BODY if self.path == "/v1/live/reply" else 4096
            if not 0 <= length <= limit:
                return self._respond(413, {"error": "body_too_large"})
            data = json.loads(self.rfile.read(length))
            if self.path == "/v1/live/read":
                result = self.server.live_reads.submit(data)
            elif self.path == "/v1/live/stop":
                if data != {}:
                    raise ValueError("invalid_live_request")
                result = self.server.live_reads.stop()
            elif self.path == "/v1/live/poll":
                if data != {}:
                    raise ValueError("invalid_live_request")
                result = {"request": self.server.live_reads.poll()}
            else:
                if not isinstance(data, dict) or set(data) != {"id", "result"} or not isinstance(data["id"], str):
                    raise ValueError("invalid_live_result")
                self.server.live_reads.reply(data["id"], data["result"])
                result = {"accepted": True}
        except (ValueError, UnicodeError):
            return self._respond(400, {"error": "invalid_live_payload"})
        return self._respond(200, result)

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


def make_server(token: str, port: int = 8765, *, live_reads: bool = False) -> ThreadingHTTPServer:
    """Create an authenticated listener bound strictly to IPv4 loopback."""
    if len(token) < 32:
        raise ValueError("Bridge token must contain at least 32 characters")
    if not 0 <= port <= 65535:
        raise ValueError("Invalid port")
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.live_reads = LiveReads(enabled=live_reads)
    server.bridge_token = token
    server.collections = None
    server.collections_updated_at = 0.0
    server.selection = None
    server.selection_updated_at = 0.0
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

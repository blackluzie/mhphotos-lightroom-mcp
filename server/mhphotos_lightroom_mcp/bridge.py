"""Local bridge between the MCP server and the Lightroom Classic plugin.

Lightroom plugins can act as HTTP clients but not as servers, so the plugin
long-polls this loopback-only HTTP endpoint for commands and posts results back.

Security: binds 127.0.0.1 only, every request needs a bearer token (constant-time
compare), request bodies are size-limited, and commands come only from the MCP
server, never from the plugin side.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import queue
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .adapters import Collection, NotFoundError, Photo, PhotoFilter

MAX_BODY = 4_000_000  # previews arrive base64-encoded
DEFAULT_PORT = 47831


class BridgeUnavailable(RuntimeError):
    """Lightroom plugin is not connected or did not answer in time."""


class BridgeError(RuntimeError):
    """The plugin reported an error while executing a command."""


class _Command:
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        self.id = uuid.uuid4().hex
        self.name = name
        self.args = args
        self.done = threading.Event()
        self.cancelled = False
        self.ok = False
        self.data: Any = None
        self.error: str | None = None


class Bridge:
    def __init__(
        self,
        token: str | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        poll_wait: float = 10.0,
        connected_ttl: float = 30.0,
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Bridge must bind to loopback only")
        self.token = token or secrets.token_urlsafe(32)
        self._poll_wait = poll_wait
        self._ttl = connected_ttl
        self._queue: queue.Queue[_Command] = queue.Queue()
        self._inflight: dict[str, _Command] = {}
        self._lock = threading.Lock()
        self._last_seen = 0.0
        self._stop = threading.Event()
        self._httpd = ThreadingHTTPServer((host, port), self._make_handler())
        self._thread: threading.Thread | None = None

    # --- lifecycle ---
    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()

    # --- state ---
    def is_connected(self) -> bool:
        with self._lock:
            busy = bool(self._inflight)
        return busy or (time.monotonic() - self._last_seen) < self._ttl

    def _touch(self) -> None:
        self._last_seen = time.monotonic()

    # --- calls from the MCP side ---
    def call(self, name: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        if not self.is_connected():
            raise BridgeUnavailable(
                "Lightroom plugin is not connected. Start Lightroom Classic and enable "
                "the MHPhotos plugin."
            )
        cmd = _Command(name, args or {})
        self._queue.put(cmd)
        if not cmd.done.wait(timeout):
            cmd.cancelled = True
            with self._lock:
                self._inflight.pop(cmd.id, None)
            raise BridgeUnavailable(f"Lightroom did not answer '{name}' within {timeout:.0f}s.")
        if not cmd.ok:
            raise BridgeError(cmd.error or "unknown plugin error")
        return cmd.data

    # --- HTTP side (plugin) ---
    def _next_command(self) -> _Command | None:
        deadline = time.monotonic() + self._poll_wait
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                cmd = self._queue.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if cmd.cancelled:
                continue
            with self._lock:
                self._inflight[cmd.id] = cmd
            return cmd
        return None

    def _handle_result(self, body: dict[str, Any]) -> bool:
        with self._lock:
            cmd = self._inflight.pop(str(body.get("id")), None)
        if cmd is None:
            return False
        cmd.ok = bool(body.get("ok"))
        cmd.data = body.get("data")
        cmd.error = body.get("error")
        cmd.done.set()
        return True

    def _make_handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # keep stdout/stderr quiet
                pass

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self) -> bool:
                auth = self.headers.get("Authorization", "")
                expected = f"Bearer {bridge.token}"
                return hmac.compare_digest(auth.encode(), expected.encode())

            def _body(self) -> dict[str, Any] | None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return None
                if length < 0 or length > MAX_BODY:
                    self.close_connection = True  # body is not read; do not reuse the socket
                    return None
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(raw or b"{}")
                except ValueError:
                    return None
                if data == []:
                    # The plugin's JSON encoder writes an empty Lua table as [] (it cannot tell an
                    # empty object from an empty array), so an empty list means "no fields".
                    return {}
                return data if isinstance(data, dict) else None

            def do_GET(self) -> None:
                if not self._authorized():
                    return self._send(401, {"error": "unauthorized"})
                if self.path == "/v1/health":
                    return self._send(200, {"ok": True, "plugin_connected": bridge.is_connected()})
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                if not self._authorized():
                    return self._send(401, {"error": "unauthorized"})
                body = self._body()
                if body is None:
                    return self._send(400, {"error": "bad request"})
                if self.path == "/v1/poll":
                    bridge._touch()
                    cmd = bridge._next_command()
                    bridge._touch()
                    if cmd is None:
                        return self._send(200, {"command": None})
                    return self._send(
                        200, {"command": {"id": cmd.id, "name": cmd.name, "args": cmd.args}}
                    )
                if self.path == "/v1/result":
                    bridge._touch()
                    known = bridge._handle_result(body)
                    return self._send(200 if known else 404, {"ok": known})
                self._send(404, {"error": "not found"})

        return Handler


def config_dir() -> Path:
    override = os.environ.get("MHPHOTOS_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".mhphotos-lightroom-mcp"


def write_plugin_config(bridge: Bridge) -> Path:
    """Write url+token for the Lua plugin (owner-only permissions)."""
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bridge.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"url": bridge.url, "token": bridge.token}, fh)
    os.chmod(path, 0o600)
    return path


class BridgeAdapter:
    """CatalogAdapter that forwards every operation to the Lightroom plugin."""

    name = "bridge"

    def __init__(self, bridge: Bridge, timeout: float = 30.0, export_timeout: float = 600.0) -> None:
        self._bridge = bridge
        self._timeout = timeout
        self._export_timeout = export_timeout

    def _call(self, name: str, args: dict[str, Any] | None = None, timeout: float | None = None):
        try:
            return self._bridge.call(name, args, timeout or self._timeout)
        except BridgeError as exc:
            if str(exc).startswith("not_found:"):
                raise NotFoundError(str(exc).split(":", 1)[1].strip()) from None
            raise

    def is_connected(self) -> bool:
        return self._bridge.is_connected()

    def list_collections(self, include_counts: bool = False) -> list[Collection]:
        data = self._call(
            "list_collections",
            {"include_counts": include_counts},
            timeout=120.0 if include_counts else None,  # counting walks every photo
        )
        return [
            Collection(str(c["id"]), str(c["name"]), photo_count=int(c.get("photo_count", 0)))
            for c in data["collections"]
        ]

    def selftest(self) -> dict[str, Any]:
        return self._call("selftest", timeout=180.0)

    def list_photos(self, collection_id, limit, offset, filters=None):
        data = self._call(
            "list_photos",
            {
                "collection_id": collection_id,
                "limit": limit,
                "offset": offset,
                "filters": filters.to_args() if isinstance(filters, PhotoFilter) else {},
            },
        )
        return [Photo.from_dict(p) for p in data["photos"]], int(data["total"])

    def get_photo(self, photo_id: str) -> Photo:
        return Photo.from_dict(self._call("get_photo", {"photo_id": photo_id}))

    def get_preview(self, photo_id, size):
        data = self._call("get_preview", {"photo_id": photo_id, "size": size}, timeout=60.0)
        return base64.b64decode(data["image_base64"])

    def get_develop_settings(self, photo_id):
        settings = self._call("get_develop_settings", {"photo_id": photo_id})["settings"]
        return settings if isinstance(settings, dict) else {}  # empty Lua table encodes as []

    def list_presets(self):
        return self._call("list_presets")["presets"]

    def apply_preset(self, photo_ids, preset):
        return self._call("apply_preset", {"photo_ids": photo_ids, "preset": preset})

    def apply_develop_settings(self, photo_ids, settings):
        return self._call("apply_develop_settings", {"photo_ids": photo_ids, "settings": settings})

    def create_preset(self, name, settings):
        return self._call("create_preset", {"name": name, "settings": settings})

    def create_collection(self, name):
        return self._call("create_collection", {"name": name})

    def add_to_collection(self, collection_id, photo_ids):
        return self._call("add_to_collection", {"collection_id": collection_id, "photo_ids": photo_ids})

    def set_color_label(self, photo_ids, label):
        return self._call("set_color_label", {"photo_ids": photo_ids, "label": label})

    def set_title_caption(self, photo_ids, title, caption):
        args: dict[str, Any] = {"photo_ids": photo_ids}
        if title is not None:
            args["title"] = title
        if caption is not None:
            args["caption"] = caption
        return self._call("set_title_caption", args)

    def remove_keywords(self, photo_ids, keywords):
        return self._call("remove_keywords", {"photo_ids": photo_ids, "keywords": keywords})

    def create_virtual_copies(self, photo_ids):
        return self._call("create_virtual_copies", {"photo_ids": photo_ids}, timeout=120.0)

    def set_rating(self, photo_ids, rating):
        return self._call("set_rating", {"photo_ids": photo_ids, "rating": rating})

    def set_flag(self, photo_ids, flag):
        return self._call("set_flag", {"photo_ids": photo_ids, "flag": flag})

    def add_keywords(self, photo_ids, keywords):
        return self._call("add_keywords", {"photo_ids": photo_ids, "keywords": keywords})

    def export_photos(self, photo_ids, dest_dir):
        return self._call(
            "export_photos",
            {"photo_ids": photo_ids, "dest_dir": dest_dir},
            timeout=self._export_timeout,
        )

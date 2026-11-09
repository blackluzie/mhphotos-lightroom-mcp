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

MAX_BODY = 24_000_000  # previews arrive base64-encoded (get_previews: up to 30 at once)
DEFAULT_PORT = 47831


class BridgeUnavailable(RuntimeError):
    """Lightroom plugin is not connected or did not answer in time."""


class BridgeError(RuntimeError):
    """The plugin reported an error while executing a command."""


class _Command:
    def __init__(self, name: str, args: dict[str, Any], timeout: float = 30.0) -> None:
        self.id = uuid.uuid4().hex
        self.name = name
        self.args = args
        self.deadline = time.monotonic() + timeout
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
        reconnect_wait: float = 0.0,
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Bridge must bind to loopback only")
        self.token = token or secrets.token_urlsafe(32)
        self._poll_wait = poll_wait
        self._ttl = connected_ttl
        # How long call() waits for the plug-in to come back before it reports "not connected". Since plug-in
        # 0.1.28 a watchdog in Lightroom replaces a polling task that hangs in a Lightroom call; waiting here
        # turns such a hiccup into a slower answer instead of an error the user has to fix by hand.
        self._reconnect_wait = reconnect_wait
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
    def wait_connected(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self.is_connected():
            if time.monotonic() >= deadline or self._stop.is_set():
                return False
            time.sleep(0.25)
        return True

    def call(self, name: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        if not self.is_connected() and not self.wait_connected(self._reconnect_wait):
            raise BridgeUnavailable(
                "Lightroom plugin is not connected. Start Lightroom Classic and enable "
                "the MHPhotos plugin." + (
                    f" (Waited {self._reconnect_wait:.0f}s for it to reconnect.)" if self._reconnect_wait else ""
                )
            )
        cmd = _Command(name, args or {}, timeout)
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
                    # seconds left until call() gives up: the plugin answers (with an error if
                    # need be) before that and keeps polling, instead of going silent
                    left = max(1, int(cmd.deadline - time.monotonic()))
                    return self._send(
                        200,
                        {"command": {"id": cmd.id, "name": cmd.name, "args": cmd.args, "timeout": left}},
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


def _copy_job_result(job: dict[str, Any], raise_errors: bool = True) -> dict[str, Any]:
    """The plug-in's job status as a tool result: {"copies", "job_id", "state", ...}."""
    copies = [{"source_id": c.get("source_id"), "id": c.get("id")} for c in job.get("copies") or []]
    out: dict[str, Any] = {"copies": copies, "job_id": job.get("job_id"), "state": job.get("state")}
    state = job.get("state")
    if state == "running":
        out.update(
            phase=job.get("phase"), total=job.get("total"), elapsed_s=job.get("elapsed"),
            hint="Lightroom is still making the copies (the bridge stays connected). Ask virtual_copy_job with this "
            "job_id every 30-60 s until state is done; new copies answer slowly for a few minutes (photo_busy).",
        )
    elif state != "done":
        made = f" Copies made: {', '.join(c['id'] for c in copies)}." if copies else ""
        message = f"virtual_copy_{state}: {job.get('error') or 'no copy was made'}{made}"
        if raise_errors:
            raise BridgeError(message)
        out.update(error=message, missing=job.get("missing") or [])
    return out


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


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _unknown_command(exc: Exception) -> bool:
    return "unknown command" in str(exc)


class BridgeAdapter:
    """CatalogAdapter that forwards every operation to the Lightroom plugin."""

    # Writes on many photos go to Lightroom in parts of this size, each with its own time limit: one call with
    # 100 photos ran past the 30 s limit (45 worked), and the bridge then looked disconnected.
    write_chunk = 25
    preview_chunk = 10

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

    def get_photos(self, photo_ids: list[str]) -> list[Photo | None]:
        """Metadata of many photos in one bridge round per 100 (plug-in 0.1.28+); None = not found.

        Raises NotImplementedError for an older plug-in, so callers fall back to get_photo.
        """
        out: list[Photo | None] = []
        for chunk in _chunks(photo_ids, 100):
            try:
                data = self._call("get_photos", {"photo_ids": chunk}, timeout=30.0 + 0.5 * len(chunk))
            except BridgeError as exc:
                if _unknown_command(exc):
                    raise NotImplementedError("plug-in before 0.1.28") from None
                raise
            for item in data["photos"]:
                out.append(None if item.get("error") else Photo.from_dict(item))
        return out

    def get_previews(self, photo_ids: list[str], size: int) -> list[bytes | None]:
        """JPEG previews, rendered in parallel by Lightroom (plug-in 0.1.28+); None = not rendered in time."""
        out: list[bytes | None] = []
        for chunk in _chunks(photo_ids, self.preview_chunk):
            try:
                data = self._call("get_previews", {"photo_ids": chunk, "size": size, "wait": 25}, timeout=45.0)
            except BridgeError as exc:
                if _unknown_command(exc):
                    raise NotImplementedError("plug-in before 0.1.28") from None
                raise
            for item in data["previews"]:
                b64 = item.get("image_base64")
                out.append(base64.b64decode(b64) if b64 else None)
        return out

    def _write(self, name: str, photo_ids: list[str], args: dict[str, Any]) -> dict[str, Any]:
        """A write on photo_ids, sent in parts of write_chunk photos; sums the parts' "updated"."""
        done: list[str] = []
        updated = 0
        for chunk in _chunks(photo_ids, self.write_chunk):
            try:
                res = self._call(name, {"photo_ids": chunk, **args}, timeout=self._timeout + 0.5 * len(chunk))
            except (BridgeError, BridgeUnavailable) as exc:
                if not done:
                    raise
                raise type(exc)(
                    f"{exc} (the first {len(done)} of {len(photo_ids)} photos were already changed: "
                    f"{', '.join(done)}; the rest is unchanged)"
                ) from None
            done.extend(chunk)
            updated += int((res or {}).get("updated", len(chunk))) if isinstance(res, dict) else len(chunk)
        return {"updated": updated}

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

    def create_collection(self, name, parent=None):
        return self._call("create_collection", {"name": name, **({"parent": parent} if parent else {})})

    def move_collection(self, collection_id, parent):
        return self._call("move_collection", {"collection_id": collection_id, "parent": parent})

    def create_smart_collection(self, name, parent, rules):
        return self._call("create_smart_collection", {"name": name, "rules": rules, **({"parent": parent} if parent else {})})

    def create_keyword(self, name, synonyms, include_on_export):
        return self._call("create_keyword", {"name": name, "synonyms": synonyms, "include_on_export": include_on_export})

    def build_smart_previews(self, photo_ids):
        return self._call("build_smart_previews", {"photo_ids": photo_ids})

    def import_photo(self, path, collection_id):
        return self._call("add_photo", {"path": path, **({"collection_id": collection_id} if collection_id else {})})

    def add_to_collection(self, collection_id, photo_ids):
        return self._write("add_to_collection", photo_ids, {"collection_id": collection_id})

    def remove_from_collection(self, collection_id, photo_ids):
        return self._write("remove_from_collection", photo_ids, {"collection_id": collection_id})

    def set_color_label(self, photo_ids, label):
        return self._write("set_color_label", photo_ids, {"label": label})

    def set_title_caption(self, photo_ids, title, caption):
        args: dict[str, Any] = {}
        if title is not None:
            args["title"] = title
        if caption is not None:
            args["caption"] = caption
        return self._write("set_title_caption", photo_ids, args)

    def remove_keywords(self, photo_ids, keywords):
        return self._write("remove_keywords", photo_ids, {"keywords": keywords})

    # Since plug-in 0.1.25 create_virtual_copies only starts a job in Lightroom and answers at once; the copies are
    # made in a task of their own (Lightroom's call never comes back, and new copies read slowly for minutes). The
    # server asks for the job's status (answered from memory, never blocks) until it ends or copy_wait runs out, so
    # no single bridge call and no MCP call waits for Lightroom longer than about 20 s.
    copy_wait = 20.0
    copy_poll_every = 1.0

    def create_virtual_copies(self, photo_ids, copy_name=None, wait=None):
        args = {"photo_ids": photo_ids, **({"copy_name": copy_name} if copy_name else {})}
        job = self._call("create_virtual_copies", args, timeout=30.0)
        if not isinstance(job, dict) or "job_id" not in job:
            return job  # a plug-in before 0.1.25 answers with the copies themselves
        deadline = time.monotonic() + (self.copy_wait if wait is None else wait)
        while job.get("state") == "running" and time.monotonic() < deadline:
            time.sleep(min(self.copy_poll_every, max(0.0, deadline - time.monotonic())))
            job = self._call("virtual_copy_job", {"job_id": job["job_id"]}, timeout=15.0)
        return _copy_job_result(job)

    def virtual_copy_job(self, job_id):
        return _copy_job_result(self._call("virtual_copy_job", {"job_id": job_id}, timeout=15.0), raise_errors=False)

    def photo_dimensions(self, photo_ids):
        return self._call("photo_dimensions", {"photo_ids": photo_ids})

    def set_rating(self, photo_ids, rating):
        return self._write("set_rating", photo_ids, {"rating": rating})

    def set_flag(self, photo_ids, flag):
        return self._write("set_flag", photo_ids, {"flag": flag})

    def add_keywords(self, photo_ids, keywords):
        return self._write("add_keywords", photo_ids, {"keywords": keywords})

    def audit_page(self, offset, limit, refresh=False, year=None):
        # the first page also reads the catalog's photo list (seconds for 250k+ photos); with a year the
        # capture times and paths of the whole catalog are read first (fast batches), details only for the year
        args = {"offset": offset, "limit": limit, "refresh": refresh}
        if year:
            args["year"] = year
        return self._call("audit_page", args, timeout=600.0)

    def audit_scan(self, year, start):
        # one step of the year scan; the first step reads the photo list of the whole catalog
        return self._call("audit_scan", {"year": year, "start": start}, timeout=600.0)

    def create_review_collections(self, root, groups):
        return self._call("create_review_collections", {"root": root, "groups": groups}, timeout=600.0)

    def keyword_tree(self, offset, limit, counts=False):
        # counting the photos of every keyword can take a while on a big catalog
        return self._call("keyword_tree", {"offset": offset, "limit": limit, "counts": counts}, timeout=300.0)

    def merge_keywords(self, source_ids, target_id, max_photos=5000):
        return self._call("merge_keywords", {"source_ids": source_ids, "target_id": target_id,
                                             "max_photos": max_photos}, timeout=300.0)

    def photos_by_date(self, captured_from, captured_to, offset, limit, refresh=False):
        # the first query builds a capture-date index of the whole catalog (can take a while)
        return self._call("photos_by_date", {"captured_from": captured_from, "captured_to": captured_to,
                                             "offset": offset, "limit": limit, "refresh": refresh}, timeout=300.0)

    def set_location(self, photo_ids, latitude, longitude, place):
        args: dict[str, Any] = {"photo_ids": photo_ids, **place}
        if latitude is not None:
            args.update(latitude=latitude, longitude=longitude)
        return self._call("set_location", args)

    def rotate_photos(self, photo_ids, degrees):
        return self._call("rotate_photos", {"photo_ids": photo_ids, "degrees": degrees})

    def photo_paths(self, photo_ids):
        return self._call("photo_paths", {"photo_ids": photo_ids})

    def list_webhooks(self):
        return self._call("webhook_list")

    def send_webhook(self, name, payload):
        return self._call("webhook_send", {"name": name, "payload": payload}, timeout=40)

    def set_custom_metadata(self, photo_ids, status, note):
        args = {}
        if status is not None:
            args["status"] = status
        if note is not None:
            args["note"] = note
        return self._write("set_custom_metadata", photo_ids, args)

    def auto_tone(self, photo_ids, white_balance):
        return self._call("auto_tone", {"photo_ids": photo_ids, "white_balance": white_balance}, timeout=120)

    def export_photos(self, photo_ids, dest_dir, options=None):
        return self._call(
            "export_photos",
            {"photo_ids": photo_ids, "dest_dir": dest_dir, **(options or {})},
            timeout=self._export_timeout,
        )

"""Client for the MHPhotos admin API (Foto-Inbox, galleries, Instagram library).

Uploads land in the Foto-Inbox with status "wartet" (review queue). Only ``approve``
puts a photo on the public site, and the MCP server gates it separately.
Auth: header X-Admin-Token, token and URL come from the operator's environment.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

URL_ENV = "MHPHOTOS_INBOX_URL"
TOKEN_ENV = "MHPHOTOS_ADMIN_TOKEN"
PUBLISH_ENV = "MHPHOTOS_ALLOW_PUBLISH"
MAX_FILE_BYTES = 24 * 1024 * 1024  # the inbox rejects larger originals
ENDPOINT = "/api/admin/lightroom-import"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class InboxConfig:
    base_url: str
    token: str

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + ENDPOINT

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.base_url).netloc


def inbox_config() -> InboxConfig | None:
    """Operator-provided target. https only (plain http just for localhost)."""
    url = os.environ.get(URL_ENV, "").strip()
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not url or not token:
        return None
    parsed = urllib.parse.urlparse(url)
    local_http = parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS
    if (parsed.scheme != "https" and not local_http) or not parsed.hostname:
        return None
    return InboxConfig(url, token)


def disabled_message() -> str:
    return (
        f"Publishing is disabled. The operator must set {URL_ENV} (https URL of the "
        f"MHPhotos app) and {TOKEN_ENV}, plus MHPHOTOS_EXPORT_DIR."
    )


def _multipart(fields: dict[str, str], filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += value.encode() + b"\r\n"
    safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
    out += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe}"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode()
    out += data + b"\r\n" + f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def upload(cfg: InboxConfig, path: Path, meta: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    """Upload one JPEG. Returns {"status": "uploaded"|"duplicate"|"error", ...}."""
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        return {"status": "error", "error": "file larger than 24 MB inbox limit"}
    fields = {"source": "lightroom-classic-plugin", "rating": str(meta.get("rating", 0) or 0)}
    if meta.get("keywords"):
        fields["keywords"] = ",".join(meta["keywords"])
    body, ctype = _multipart(fields, path.name, path.read_bytes())
    req = urllib.request.Request(
        cfg.endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": ctype, "X-Admin-Token": cfg.token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return {"status": "error", "error": f"inbox answered HTTP {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"status": "error", "error": f"inbox not reachable: {type(exc).__name__}"}
    item = payload.get("item") or {}
    return {
        "status": "duplicate" if payload.get("duplicate") else "uploaded",
        "inbox_id": item.get("id"),
    }


def publishing_enabled() -> bool:
    """Operator opt-in for putting photos on the public site."""
    return os.environ.get(PUBLISH_ENV, "").strip().lower() in {"1", "true", "yes"}


def publish_disabled_message() -> str:
    return f"Publishing to the public site is disabled. The operator must set {PUBLISH_ENV}=1."


def api(
    cfg: InboxConfig,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, Any]]:
    """JSON call to the MHPhotos admin API. Never raises; status 0 = not reachable."""
    data = None if body is None else json.dumps(body).encode()
    headers = {"X-Admin-Token": cfg.token, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(cfg.base_url.rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        return exc.code, payload if isinstance(payload, dict) else {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {"error": "MHPhotos app not reachable"}


def _quote(item_id: str) -> str:
    return urllib.parse.quote(item_id, safe="")


def galleries(cfg: InboxConfig) -> tuple[int, dict[str, Any]]:
    return api(cfg, "GET", "/api/admin/galleries")


def inbox_items(cfg: InboxConfig) -> tuple[int, dict[str, Any]]:
    return api(cfg, "GET", ENDPOINT)


def request_analysis(cfg: InboxConfig, item_id: str) -> tuple[int, dict[str, Any]]:
    return api(cfg, "POST", f"{ENDPOINT}/{_quote(item_id)}/analyze", {})


def approve(cfg: InboxConfig, item_id: str, fields: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Puts the item on the public site (this is the step that publishes)."""
    return api(cfg, "PATCH", f"{ENDPOINT}/{_quote(item_id)}", {"action": "approve", **fields})


def instagram_library(cfg: InboxConfig) -> tuple[int, dict[str, Any]]:
    return api(cfg, "GET", "/api/admin/instagram-library", timeout=120.0)

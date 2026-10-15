"""Send exported photos to the MHPhotos Foto-Inbox (review queue, nothing goes public).

Target: POST {base}/api/admin/lightroom-import (multipart, header X-Admin-Token).
Items land with status "wartet" and are approved inside the MHPhotos app.
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

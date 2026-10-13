"""Secure local file transport for Lightroom selection metadata."""

import json
import os
import tempfile
from pathlib import Path

from mhphotos_lightroom_mcp.private_files import (
    private_directory,
    validate_private,
    windows_acl,
)

MAX_BYTES = 8192
PREFIX = "selection-"
SUFFIX = ".json"


def transport_directory() -> Path:
    """Return a private directory for Lightroom metadata transfers."""
    path = Path.home() / ".mhphotos-lightroom" / "transfers"
    private_directory(path.parent)
    private_directory(path)

    return path


def create_transfer(payload: dict) -> Path:
    """Create a private JSON transfer file."""
    directory = transport_directory()

    data = json.dumps(payload).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("Transfer payload too large")

    fd, filename = tempfile.mkstemp(
        prefix=PREFIX,
        suffix=SUFFIX,
        dir=directory,
    )

    path = Path(filename)

    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name == "nt":
                windows_acl(path, protect=True)
            handle.write(data)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def read_transfer(path: Path) -> dict:
    """Read a private transfer file and remove it after processing."""
    directory = transport_directory()

    if (
        path.parent != directory
        or not path.name.startswith(PREFIX)
        or not path.name.endswith(SUFFIX)
    ):
        raise ValueError("Invalid transfer path")

    validate_private(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        if os.fstat(handle.fileno()).st_size > MAX_BYTES:
            raise ValueError("Transfer payload too large")
        data = handle.read(MAX_BYTES + 1)

    if len(data) > MAX_BYTES:
        raise ValueError("Transfer payload too large")

    payload = json.loads(data)

    if not isinstance(payload, dict):
        raise ValueError("Transfer payload must be an object")  # noqa: TRY004

    return payload

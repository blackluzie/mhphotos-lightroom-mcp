"""Secure local file transport for Lightroom selection metadata."""

import json
import os
import stat
import tempfile
from pathlib import Path

MAX_BYTES = 8192
PREFIX = "selection-"
SUFFIX = ".json"


def transport_directory() -> Path:
    """Return a private directory for Lightroom metadata transfers."""
    path = Path.home() / ".mhphotos-lightroom" / "transfers"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)

    if os.name == "posix":
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise PermissionError("Transfer directory must be private")

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

    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)

        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())

            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_BYTES
            ):
                raise PermissionError("Unsafe transfer file")

            data = handle.read(MAX_BYTES + 1)
    else:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Invalid transfer file")
        data = path.read_bytes()

    if len(data) > MAX_BYTES:
        raise ValueError("Transfer payload too large")

    payload = json.loads(data)

    if not isinstance(payload, dict):
        raise ValueError("Transfer payload must be an object")

    return payload

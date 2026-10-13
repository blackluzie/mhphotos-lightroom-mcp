"""Reuse the legacy credential path without ever disclosing or rotating it."""
import os
import secrets
import stat
from pathlib import Path

from mhphotos_lightroom_mcp.private_files import (
    reject_links,
    validate_private,
    windows_acl,
)


def read_bridge_token(path: Path) -> str:
    validate_private(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            raise ValueError('Invalid bridge token file')
        token = handle.read(4097).strip()
    if not 32 <= len(token) <= 1024 or any(char.isspace() for char in token):
        raise ValueError('Invalid bridge token file format')
    return token


def ensure_bridge_token(path: Path | None = None) -> str:
    path = path or Path.home() / '.mhphotos-lightroom-token'
    reject_links(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return read_bridge_token(path)
    # Windows starts with an empty file. Protect it before writing the secret.
    with os.fdopen(fd, 'w') as handle:
        if os.name == 'nt':
            windows_acl(path, protect=True)
        handle.write(secrets.token_urlsafe(48) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    return read_bridge_token(path)

"""Permission gate for everything that changes data.

Reads are always allowed. Writes need BOTH an operator opt-in (environment,
set outside the MCP conversation) AND ``confirm=true`` on the individual call.
Without ``confirm`` a write tool only returns a preview (dry run).
"""

from __future__ import annotations

import os
from pathlib import Path

WRITES_ENV = "MHPHOTOS_ALLOW_WRITES"
EXPORT_DIR_ENV = "MHPHOTOS_EXPORT_DIR"


def writes_enabled() -> bool:
    return os.environ.get(WRITES_ENV, "").strip().lower() in {"1", "true", "yes"}


def export_dir() -> str | None:
    """Configured export destination (absolute, created on demand), or None."""
    raw = os.environ.get(EXPORT_DIR_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return None
    return str(path)


def writes_disabled_message() -> str:
    return f"Edits are disabled. The operator must set {WRITES_ENV}=1 to allow them."


def export_disabled_message() -> str:
    return (
        f"Export is disabled. The operator must set {EXPORT_DIR_ENV} to an absolute "
        "folder path; clients cannot choose the destination."
    )

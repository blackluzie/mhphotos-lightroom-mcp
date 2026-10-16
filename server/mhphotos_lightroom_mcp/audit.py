"""Local audit log of every confirmed change (JSON lines, owner-only permissions).

Records what changed, for which photos, the values before (so some changes can be undone),
and how it was approved. Never stores tokens or file contents.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bridge import config_dir

MAX_BYTES = 5 * 1024 * 1024


def log_path() -> Path:
    return config_dir() / "audit.log"


def record(
    tool: str,
    photo_ids: list[str],
    change: dict[str, Any],
    *,
    before: dict[str, dict[str, Any]] | None = None,
    undoable: bool = False,
    approved_by: str = "flag",
    result: dict[str, Any] | None = None,
    undoes: str | None = None,
) -> str:
    entry = {
        "id": uuid.uuid4().hex[:10],
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": tool,
        "photo_ids": photo_ids,
        "change": change,
        "before": before or {},
        "undoable": undoable,
        "approved_by": approved_by,
        "result": result or {},
        "undoes": undoes,
    }
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_BYTES:
        path.replace(path.with_suffix(".log.1"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry["id"]


def read_all() -> list[dict[str, Any]]:
    path = log_path()
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue  # a damaged line must not hide the rest
        if isinstance(item, dict) and "id" in item:
            entries.append(item)
    return entries


def undone_ids(entries: list[dict[str, Any]]) -> set[str]:
    return {e["undoes"] for e in entries if e.get("undoes")}


def last_undoable() -> dict[str, Any] | None:
    entries = read_all()
    done = undone_ids(entries)
    for entry in reversed(entries):
        if entry.get("undoable") and entry["id"] not in done and not entry.get("undoes"):
            return entry
    return None


def recent(limit: int = 10) -> list[dict[str, Any]]:
    entries = read_all()
    done = undone_ids(entries)
    out = []
    for e in reversed(entries[-max(1, limit):]):
        out.append(
            {
                "id": e["id"],
                "time": e.get("ts"),
                "tool": e.get("tool"),
                "photos": len(e.get("photo_ids") or []),
                "change": e.get("change"),
                "approved_by": e.get("approved_by"),
                "undoable": bool(e.get("undoable")) and e["id"] not in done and not e.get("undoes"),
                "undone": e["id"] in done,
                "undoes": e.get("undoes"),
            }
        )
    return out

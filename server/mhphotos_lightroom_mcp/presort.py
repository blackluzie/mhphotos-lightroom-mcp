"""Pre-sorting unreviewed photos month by month (for a nightly run by Claude).

The server cannot look at photos itself; Claude does that with contact sheets. This module only prepares the
work (unreviewed photos of a month, grouped into series of shots taken seconds apart, cut into contact-sheet
batches that keep a series together) and remembers which months are done. The verdicts end up in review
collections below "Vorsortieren"; stars, flags and files are never changed.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import date, datetime
from typing import Any

from .bridge import config_dir

ROOT_SET = "Vorsortieren"
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
SHEET = 6  # photos per contact sheet: bigger sheets ran into time-outs with the real plug-in (skill /mhphotos)
VIDEO_EXT = {"mov", "mp4", "m4v", "avi", "mts", "3gp"}
VERDICTS = {
    "top": "Kandidat 5★",
    "good": "Kandidat 4★",
    "reject": "Aussortieren (Vorschlag)",
    "series_rest": "Serie – Rest (Vorschlag)",
}


def month_range(month: str) -> tuple[str, str] | None:
    m = MONTH_RE.match(month or "")
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    nxt = date(y + (mo == 12), mo % 12 + 1, 1)
    last = date.fromordinal(nxt.toordinal() - 1)
    return f"{y:04d}-{mo:02d}-01", last.isoformat()


def is_unreviewed(r: dict[str, Any]) -> bool:
    return not r.get("r") and not r.get("pick")


def is_video(r: dict[str, Any]) -> bool:
    name = str(r.get("file") or "")
    return "." in name and name.rsplit(".", 1)[1].lower() in VIDEO_EXT


def _when(r: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(r.get("t") or "")[:19])
    except ValueError:
        return None


def series(recs: list[dict[str, Any]], gap_seconds: int = 5) -> list[list[dict[str, Any]]]:
    """Shots of the same camera taken at most gap_seconds apart form one series (bursts, repeated attempts)."""
    ordered = sorted((r for r in recs if _when(r)), key=lambda r: (str(r.get("t")), str(r.get("file") or "")))
    out: list[list[dict[str, Any]]] = []
    for r in ordered:
        if out:
            prev = out[-1][-1]
            gap = (_when(r) - _when(prev)).total_seconds()  # type: ignore[operator]
            if (r.get("cam") or "") == (prev.get("cam") or "") and 0 <= gap <= gap_seconds:
                out[-1].append(r)
                continue
        out.append([r])
    return out


def batches(groups: list[list[dict[str, Any]]], size: int = SHEET) -> list[dict[str, Any]]:
    """Contact-sheet batches of up to size photos; a series is not split unless it alone is bigger."""
    out: list[dict[str, Any]] = []
    cur: list[list[str]] = []
    used = 0

    def flush() -> None:
        nonlocal cur, used
        if cur:
            out.append({"photo_ids": [i for g in cur for i in g], "series": [g for g in cur if len(g) > 1]})
        cur, used = [], 0

    for g in groups:
        ids = [r["id"] for r in g]
        if len(ids) > size:
            flush()
            for start in range(0, len(ids), size):
                part = ids[start:start + size]
                out.append({"photo_ids": part, "series": [part] if len(part) > 1 else []})
            continue
        if used + len(ids) > size:
            flush()
        cur.append(ids)
        used += len(ids)
    flush()
    for n, b in enumerate(out, 1):
        b["batch"] = n
    return out


def plan(recs: list[dict[str, Any]], month: str, gap_seconds: int = 5, sheet: int = SHEET) -> dict[str, Any]:
    photos = [r for r in recs if not is_video(r)]
    todo = [r for r in photos if is_unreviewed(r)]
    groups = series(todo, gap_seconds)
    work = batches(groups, sheet)
    return {
        "month": month, "photos_in_month": len(photos), "unreviewed": len(todo),
        "series": sum(1 for g in groups if len(g) > 1), "photos_in_series": sum(len(g) for g in groups if len(g) > 1),
        "batches_total": len(work), "batches": work,
    }


# ---------------------------------------------------------------- progress
def progress_path():
    return config_dir() / "presort" / "progress.json"


def load_progress() -> dict[str, Any]:
    try:
        data = json.loads(progress_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(month: str, counts: dict[str, int], finished: bool, batch: int | None = None) -> dict[str, Any]:
    data = load_progress()
    entry = data.get(month) or {"counts": {}}
    for k, v in counts.items():
        entry["counts"][k] = int(entry["counts"].get(k) or 0) + int(v)
    entry["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if batch is not None:
        entry["last_batch"] = max(int(entry.get("last_batch") or 0), batch)
    if finished:
        entry["done"] = True
    data[month] = entry
    path = progress_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return entry


def unreviewed_by_month(photos: list[dict[str, Any]]) -> Counter[str]:
    """From the catalog snapshot: unreviewed photos (no stars, no flag; videos left out) per capture month."""
    out: Counter[str] = Counter()
    for p in photos:
        t = str(p.get("t") or "")
        if len(t) >= 7 and not p.get("r") and not p.get("pick") and (p.get("fmt") or "").upper() != "VIDEO":
            out[t[:7]] += 1
    return out


def next_months(progress: dict[str, Any], backlog: Counter[str], count: int = 3) -> list[dict[str, Any]]:
    """Newest months with unreviewed photos that are not done yet (a started month comes first)."""
    open_months = [m for m, n in backlog.items() if n and not (progress.get(m) or {}).get("done")]
    started = [m for m in open_months if (progress.get(m) or {}).get("last_batch")]
    rest = sorted((m for m in open_months if m not in started), reverse=True)
    return [{"month": m, "unreviewed": backlog[m], **({"last_batch": progress[m]["last_batch"]} if m in started else {})}
            for m in (sorted(started, reverse=True) + rest)[:count]]

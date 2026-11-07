"""Keyword clean-up check (read only): duplicate, variant, unused and loose keywords.

A background run reads the whole keyword list with the number of photos per keyword (plug-in command
keyword_tree, 0.1.26+) and writes the result to ``<config>/keywords/latest.json``. Merging is a separate,
gated tool (merge_keywords); deleting a keyword is not possible through the SDK (Lightroom: Metadata >
Purge Unused Keywords).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from .bridge import config_dir

PAGE = 500
KINDS = ("duplicates", "variants", "same_name_elsewhere", "unused", "loose")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_PLURAL = re.compile(r"(?:en|er|e|n|s)$")


def norm(name: str) -> str:
    """'  Café-Bar ' and 'cafe bar' are one name; 'Bäume' and 'baeume' too."""
    text = (name or "").strip().casefold().translate(_UMLAUTS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def stem(name: str) -> str:
    n = norm(name)
    return _PLURAL.sub("", n) if len(n) > 4 else n


def _parent_path(kw: dict[str, Any]) -> str:
    path = kw.get("path") or kw.get("name") or ""
    return norm(path.rsplit(" > ", 1)[0]) if " > " in path else ""


def _brief(kw: dict[str, Any]) -> dict[str, Any]:
    out = {"id": kw["id"], "path": kw.get("path") or kw.get("name"), "photos": kw.get("photos")}
    if kw.get("children"):
        out["children"] = kw["children"]
    return out


def _target(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep the keyword inside a hierarchy with the most photos (then the deepest, then the shortest name)."""
    return min(group, key=lambda k: (-(k.get("photos") or 0), -(k.get("depth") or 0), len(k.get("path") or "")))


def analyse(keywords: list[dict[str, Any]]) -> dict[str, Any]:
    findings: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for kw in keywords:
        if norm(kw.get("name") or ""):
            by_name[norm(kw["name"])].append(kw)
    grouped_ids: set[str] = set()
    for name, group in by_name.items():
        if len(group) < 2:
            continue
        # same parent, or a loose top-level keyword next to the same name inside a hierarchy: merge candidates;
        # the same name in different branches (Orte > Spanien > Valencia, Orte > Venezuela > Valencia) is only shown
        parents: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for kw in group:
            parents[_parent_path(kw)].append(kw)
        loose = [k for k in group if not k.get("depth") and not k.get("children")]
        nested = [k for k in group if k.get("depth")]
        merged = False
        for same in parents.values():
            if len(same) > 1:
                tgt = _target(same)
                findings["duplicates"].append({"name": name, "reason": "same place in the tree", "keep": _brief(tgt),
                                               "merge": [_brief(k) for k in same if k is not tgt]})
                grouped_ids.update(k["id"] for k in same)
                merged = True
        if loose and nested and len({_parent_path(k) for k in nested}) == 1:
            tgt = _target(nested)
            rest = [k for k in loose if k["id"] not in grouped_ids]
            if rest:
                findings["duplicates"].append({"name": name, "reason": "loose keyword next to the same one in a hierarchy",
                                               "keep": _brief(tgt), "merge": [_brief(k) for k in rest]})
                grouped_ids.update(k["id"] for k in rest + [tgt])
                merged = True
        if not merged:
            findings["same_name_elsewhere"].append({"name": name, "keywords": [_brief(k) for k in group]})
    by_stem: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for kw in keywords:
        if kw["id"] not in grouped_ids and norm(kw.get("name") or ""):
            by_stem[(_parent_path(kw), stem(kw["name"]))].append(kw)
    for (_parent, st), group in by_stem.items():
        names = {norm(k["name"]) for k in group}
        if len(group) > 1 and len(names) > 1:
            tgt = _target(group)
            findings["variants"].append({"stem": st, "keep": _brief(tgt), "merge": [_brief(k) for k in group if k is not tgt],
                                         "note": "singular/plural or spelling; check before merging"})
    for kw in keywords:
        if kw.get("photos") == 0 and not kw.get("children"):
            findings["unused"].append(_brief(kw))
        elif not kw.get("depth") and not kw.get("children") and (kw.get("photos") or 0) > 0:
            findings["loose"].append(_brief(kw))
    findings["loose"].sort(key=lambda k: -(k.get("photos") or 0))
    findings["duplicates"].sort(key=lambda g: -sum(k.get("photos") or 0 for k in g["merge"]))
    counts = {k: len(v) for k, v in findings.items()}
    counts["keywords"] = len(keywords)
    counts["photos_on_duplicates"] = sum(k.get("photos") or 0 for g in findings["duplicates"] for k in g["merge"])
    return {"counts": counts, "findings": findings}


def result_path():
    return config_dir() / "keywords" / "latest.json"


def load_result() -> dict[str, Any] | None:
    try:
        return json.loads(result_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_result(result: dict[str, Any]) -> None:
    path = result_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False)
    os.replace(tmp, path)


class Job:
    """Reads keyword_tree page by page (with photo counts) in a thread, then analyses and saves."""

    def __init__(self, fetch: Callable[[int, int], dict[str, Any]]) -> None:
        self.fetch = fetch
        self.state: dict[str, Any] = {"status": "running", "read": 0, "total": None, "started": time.time()}
        self.thread = threading.Thread(target=self._run, name="keyword-check", daemon=True)

    def start(self) -> Job:
        self.thread.start()
        return self

    def _run(self) -> None:
        try:
            keywords: list[dict[str, Any]] = []
            offset = 0
            while True:
                page = self.fetch(offset, PAGE)
                keywords += page.get("keywords") or []
                total, nxt = int(page["total"]), int(page["next_offset"])
                self.state.update(total=total, read=nxt)
                if nxt <= offset or nxt >= total:
                    break
                offset = nxt
            result = {"finished_at": time.strftime("%Y-%m-%d %H:%M:%S"), **analyse(keywords)}
            save_result(result)
            self.state.update(status="done", seconds=round(time.time() - self.state["started"]))
        except Exception as exc:  # noqa: BLE001 - reported through keyword_check_status
            self.state.update(status="failed", error=f"{type(exc).__name__}: {exc}")

    def status(self) -> dict[str, Any]:
        out = {k: v for k, v in self.state.items() if k != "started"}
        out["elapsed_seconds"] = round(time.time() - self.state["started"])
        return out


_lock = threading.Lock()
_job: Job | None = None


def start(fetch: Callable[[int, int], dict[str, Any]]) -> tuple[bool, Job]:
    global _job
    with _lock:
        if _job is not None and _job.state["status"] == "running":
            return False, _job
        _job = Job(fetch).start()
        return True, _job


def current() -> Job | None:
    return _job

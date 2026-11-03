"""Catalog audit: photos in the wrong date folder and duplicates (read only).

The catalog is organised as ``JJJJ/JJJJ-MM/JJJJ-MM-TT`` (folder names may carry a suffix such
as ``2021-07-14 Hochzeit``); the capture time decides where a photo belongs. A run pages through
the whole catalog in a background thread (250k+ photos take minutes), keeps only what the
analysis needs, and writes the result to ``<config>/audit/latest.json`` on the Mac. Nothing is
moved or deleted: Lightroom's SDK cannot do either, so findings become review collections.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from .bridge import config_dir

PAGE = 1000
FINGERPRINT_BYTES = 1 << 20  # head + tail of each candidate file, never the whole RAW over the NAS
MIN_YEAR = 1990
RETRY_SECONDS = 5.0
KINDS = ("wrong_folder", "no_date", "implausible_date", "unrecognised_folder", "missing_file",
         "duplicates", "possible_duplicates", "non_photo", "jpeg_with_raw", "reimported_export")
RAW_FORMATS = {"RAW", "DNG"}
JPEG_FORMATS = {"JPG", "JPEG", "HEIC", "HEIF"}
# Portrait and landscape sizes of common phone, tablet and computer screens.
SCREEN_SIZES = {frozenset(s) for s in (
    (750, 1334), (828, 1792), (1125, 2436), (1170, 2532), (1179, 2556), (1242, 2688), (1284, 2778),
    (1290, 2796), (1080, 1920), (1080, 2340), (1080, 2400), (1440, 3200), (1920, 1080), (2560, 1440),
    (1440, 900), (1366, 768), (1280, 800), (2560, 1600), (2880, 1800), (3024, 1964), (3456, 2234),
    (1620, 2160), (1640, 2360), (1668, 2388), (2048, 2732), (5120, 2880),
)}
_SCREENSHOT_NAME = re.compile(r"screen ?shot|bildschirmfoto|simulator screen|scrnli", re.IGNORECASE)
_MESSENGER_NAME = re.compile(r"-WA\d{3,}|^IMG-\d{8}-|^signal-\d{4}|^telegram|^photo_\d{4}-\d{2}-\d{2}", re.IGNORECASE)

_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MONTH = re.compile(r"^(\d{4})-(\d{2})(?![\d-]\d)(?!\d)")
_YEAR = re.compile(r"^(\d{4})(?![\d-])")


def folder_dates(path: str) -> tuple[str | None, str | None, str | None]:
    """The deepest year, month and day folder names on the path (file name excluded)."""
    year = month = day = None
    for part in PurePosixPath(path).parts[:-1]:
        if m := _DAY.match(part):
            day = m.group(0)
        elif m := _MONTH.match(part):
            month = m.group(0)
        elif (m := _YEAR.match(part)) and 1900 <= int(m.group(1)) <= 2100:
            year = m.group(1)
    return year, month, day


def short_folder(path: str, depth: int = 3) -> str:
    """The last folders of a path, enough to recognise it without the volume layout."""
    parts = PurePosixPath(path).parts[:-1]
    return "/".join(parts[-depth:])


def classify(rec: dict[str, Any], today: str | None = None) -> dict[str, Any] | None:
    """Folder finding for one photo, or None when it sits where its capture date says."""
    path, taken = rec.get("path") or "", rec.get("t") or ""
    today = today or date.today().isoformat()
    if not taken:
        return {"kind": "no_date"}
    if int(taken[:4]) < MIN_YEAR or taken[:10] > today:
        return {"kind": "implausible_date"}
    year, month, day = folder_dates(path)
    if not (year or month or day):
        return {"kind": "unrecognised_folder"}
    expected = taken[:10]
    found = day or month or year
    level = None
    if year and year != taken[:4]:
        level = "year"
    elif month and month != taken[:7]:
        level = "year" if month[:4] != taken[:4] else "month"
    elif day and day[:10] != expected:
        level = "year" if day[:4] != taken[:4] else "month" if day[:7] != taken[:7] else "day"
    if level is None:
        return None
    out: dict[str, Any] = {"kind": "wrong_folder", "level": level, "found": found, "expected": expected}
    if level == "day" and day:
        out["day_diff"] = (date.fromisoformat(expected) - date.fromisoformat(day[:10])).days
    return out


def non_photo_reasons(rec: dict[str, Any]) -> list[str]:
    """Why a picture is probably not a camera photo (screenshot, messenger, download); [] if it is one.

    No single sign decides: a screenshot name alone counts, otherwise a missing camera plus at least
    two more signs. Videos are left alone.
    """
    fmt = (rec.get("fmt") or "").upper()
    if fmt == "VIDEO":
        return []
    name = PurePosixPath(rec.get("path") or "").name
    reasons = []
    if _SCREENSHOT_NAME.search(name):
        reasons.append("Dateiname Screenshot")
    if _MESSENGER_NAME.search(name):
        reasons.append("Dateiname Messenger")
    if fmt in {"PNG", "GIF", "WEBP"}:
        reasons.append(f"Format {fmt}")
    w, h = rec.get("w"), rec.get("h")
    if w and h and frozenset((w, h)) in SCREEN_SIZES:
        reasons.append(f"Bildschirmgröße {w}×{h}")
    if w and h and max(w, h) < 1200:
        reasons.append("kleine Auflösung")
    no_camera = not rec.get("cam")
    if no_camera:
        reasons.append("keine Kamera")
    if not rec.get("exif"):
        reasons.append("keine Belichtungsdaten")
    if "Dateiname Screenshot" in reasons:
        return reasons
    if no_camera and len(reasons) >= 3:
        return reasons
    return []


def keep_score(rec: dict[str, Any], folder_ok: bool) -> int:
    """Higher = more work in it, so keep this copy of a duplicate group."""
    return (
        (4 if rec.get("edited") else 0) + int(rec.get("r") or 0) + (2 if rec.get("pick") == 1 else 0)
        + (1 if rec.get("kw") else 0) + (1 if rec.get("label") else 0) + (2 if rec.get("copies") else 0)
        + (3 if folder_ok else 0)
    )


def duplicate_key(rec: dict[str, Any]) -> tuple | None:
    """Same moment, camera, pixel size and file type. RAW+JPEG pairs differ in type."""
    if rec.get("vc"):
        return None
    if rec.get("t"):
        return ("t", rec["t"], rec.get("cam") or "", rec.get("serial") or "", rec.get("w"), rec.get("h"),
                (rec.get("fmt") or "").upper())
    name = PurePosixPath(rec.get("path") or "").name.lower()
    return ("name", name, rec.get("size")) if name and rec.get("size") else None


_COPY_SUFFIX = re.compile(r"(?:[ _-]\d{1,2}|\s*\(\d+\)|[ _-]?(?:kopie|copy))$")


def same_name(a: str, b: str) -> bool:
    """IMG_5.jpg and IMG_5-2.jpg / IMG_5 (1).jpg / IMG_5 Kopie.jpg are one name; IMG_5 and IMG_6 not."""
    x, y = PurePosixPath(a).stem.lower(), PurePosixPath(b).stem.lower()
    return x == y or _COPY_SUFFIX.sub("", x) == y or _COPY_SUFFIX.sub("", y) == x


def fingerprint(path: str) -> str | None:
    """SHA-256 over size, the first and the last MiB. None when the file cannot be read."""
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            h = hashlib.sha256(str(size).encode())
            h.update(fh.read(FINGERPRINT_BYTES))
            if size > 2 * FINGERPRINT_BYTES:
                fh.seek(-FINGERPRINT_BYTES, os.SEEK_END)
                h.update(fh.read(FINGERPRINT_BYTES))
            return h.hexdigest()
    except OSError:
        return None


def _entry(rec: dict[str, Any], **extra: Any) -> dict[str, Any]:
    out = {"id": rec["id"], "file": PurePosixPath(rec.get("path") or "").name,
           "folder": short_folder(rec.get("path") or ""), "captured": rec.get("t") or None,
           "bytes": rec.get("size")}
    return {**out, **{k: v for k, v in extra.items() if v is not None}}


def _name_clusters(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for item in items:
        home = next((c for c in clusters if any(same_name(item["path"], o["path"]) for o in c)), None)
        if home is None:
            clusters.append([item])
        else:
            home.append(item)
    return clusters


class Analysis:
    """Streaming analysis: folder findings per record, duplicate index of small tuples."""

    def __init__(self, year: int | None = None, check_files: bool = False, today: str | None = None) -> None:
        self.year = str(year) if year else None
        self.check_files = check_files
        self.today = today
        self.scanned = 0
        self.bytes_total = 0
        self.bytes_by_year: dict[str, int] = defaultdict(int)
        self.considered = 0
        self.virtual_copies = 0
        self.findings: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
        self.file_check_denied = 0
        self.unreadable = 0
        self._dupes: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        self._moments: dict[tuple, list[dict[str, Any]]] = defaultdict(list)

    def in_scope(self, rec: dict[str, Any]) -> bool:
        if not self.year:
            return True
        return (rec.get("t") or "")[:4] == self.year or folder_dates(rec.get("path") or "")[0] == self.year

    def add(self, rec: dict[str, Any]) -> None:
        self.scanned += 1
        if not rec.get("id") or not self.in_scope(rec):
            return
        if rec.get("vc"):
            self.virtual_copies += 1  # moves with its master; not a file of its own
            return
        self.considered += 1
        size = int(rec.get("size") or 0)
        self.bytes_total += size
        self.bytes_by_year[(rec.get("t") or "")[:4] or folder_dates(rec.get("path") or "")[0] or "ohne Datum"] += size
        finding = classify(rec, self.today)
        if finding:
            kind = finding.pop("kind")
            self.findings[kind].append(_entry(rec, **finding))
        if self.check_files and rec.get("path"):
            try:
                os.stat(rec["path"])
            except PermissionError:
                self.file_check_denied += 1
            except OSError:
                self.findings["missing_file"].append(_entry(rec))
        if reasons := non_photo_reasons(rec):
            self.findings["non_photo"].append(_entry(rec, reasons=reasons))
        fmt = (rec.get("fmt") or "").upper()
        if rec.get("t") and (fmt in RAW_FORMATS or fmt in JPEG_FORMATS):
            self._moments[(rec["t"], rec.get("cam") or "", rec.get("serial") or "")].append({
                "id": rec["id"], "path": rec.get("path") or "", "raw": fmt in RAW_FORMATS,
                "edge": max(rec.get("w") or 0, rec.get("h") or 0), "work": keep_score(rec, False),
                "size": rec.get("size")})
        if key := duplicate_key(rec):
            self._dupes[key].append({
                "id": rec["id"], "path": rec.get("path") or "", "size": rec.get("size"), "t": rec.get("t"),
                "score": keep_score(rec, finding is None),
            })

    def finish(self, progress: Callable[[int, int], None] | None = None) -> None:
        self.finish_pairs()
        self.finish_duplicates(progress)

    def finish_pairs(self) -> None:
        """JPEGs shot together with a RAW (camera RAW+JPEG), and exports imported again.

        A JPEG belongs to a RAW of the same second and camera with the same file name. In the RAW's
        folder at (nearly) full size it is the camera's JPEG; elsewhere or clearly smaller it is an
        export that came back into the catalog. A JPEG with work in it (stars, keywords, edits)
        while its RAW has none is marked check=True instead of being offered for removal.
        """
        for (taken, _cam, _serial), items in self._moments.items():
            raws = [i for i in items if i["raw"]]
            if not raws:
                continue
            for jpg in (i for i in items if not i["raw"]):
                raw = next((r for r in raws if same_name(jpg["path"], r["path"])), None)
                if raw is None:
                    continue
                same_folder = PurePosixPath(jpg["path"]).parent == PurePosixPath(raw["path"]).parent
                full_size = not raw["edge"] or jpg["edge"] >= 0.9 * raw["edge"]
                kind = "jpeg_with_raw" if same_folder and full_size else "reimported_export"
                entry = {"id": jpg["id"], "file": PurePosixPath(jpg["path"]).name, "bytes": jpg["size"],
                         "folder": short_folder(jpg["path"]), "captured": taken,
                         "raw": {"id": raw["id"], "file": PurePosixPath(raw["path"]).name,
                                 "folder": short_folder(raw["path"])}}
                if jpg["work"] > 0 and raw["work"] == 0:
                    entry["check"] = True
                self.findings[kind].append(entry)
        self._moments.clear()

    def finish_duplicates(self, progress: Callable[[int, int], None] | None = None) -> None:
        groups = [g for g in self._dupes.values() if len(g) > 1]
        self._dupes.clear()
        to_check = sum(len(g) for g in groups)
        done = 0
        for group in groups:
            by_size: dict[Any, list[dict[str, Any]]] = defaultdict(list)
            for item in group:
                by_size[item["size"]].append(item)
            loose = [i for same in by_size.values() if len(same) == 1 for i in same]
            for same in (s for s in by_size.values() if len(s) > 1):
                by_print: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
                for item in same:
                    fp = fingerprint(item["path"])
                    self.unreadable += fp is None
                    by_print[fp].append(item)
                    done += 1
                    if progress:
                        progress(done, to_check)
                for fp, items in by_print.items():
                    if fp is None or len(items) == 1:
                        loose += items  # unreadable or different content: only "possible"
                        continue
                    self.findings["duplicates"].append(self._group(items, verified=True))
            # Same second, camera and size but different bytes is usually a burst. Only copies that
            # also share the file name (IMG_1.jpg, IMG_1-2.jpg) are worth a look.
            for items in _name_clusters(loose):
                if len(items) > 1:
                    self.findings["possible_duplicates"].append(self._group(items, verified=False))

    @staticmethod
    def _group(items: list[dict[str, Any]], verified: bool) -> dict[str, Any]:
        ranked = sorted(items, key=lambda i: (-i["score"], len(i["path"]), i["path"]))

        def brief(i: dict[str, Any]) -> dict[str, Any]:
            return {"id": i["id"], "file": PurePosixPath(i["path"]).name, "folder": short_folder(i["path"]),
                    "bytes": i["size"]}

        return {"captured": ranked[0]["t"], "verified": verified, "keep": brief(ranked[0]),
                "remove": [brief(i) for i in ranked[1:]]}

    def summary(self) -> dict[str, Any]:
        counts = {k: len(v) for k, v in self.findings.items()}
        counts["duplicates_to_remove"] = sum(len(g["remove"]) for g in self.findings["duplicates"])
        counts["jpeg_with_raw_to_check"] = sum(1 for f in self.findings["jpeg_with_raw"] if f.get("check"))
        levels: dict[str, int] = defaultdict(int)
        by_year: dict[str, int] = defaultdict(int)
        for f in self.findings["wrong_folder"]:
            levels[f["level"]] += 1
            by_year[f["expected"][:4]] += 1
        gb = 1024 ** 3

        def total(items: list[dict[str, Any]]) -> float:
            return round(sum(int(i.get("bytes") or 0) for i in items) / gb, 2)

        storage = {
            "total_gb": round(self.bytes_total / gb, 1),
            "by_year_gb": {y: round(b / gb, 1) for y, b in sorted(self.bytes_by_year.items())},
            "freeable_gb": {
                "duplicates": total([r for g in self.findings["duplicates"] for r in g["remove"]]),
                "jpeg_with_raw": total([f for f in self.findings["jpeg_with_raw"] if not f.get("check")]),
                "reimported_export": total(self.findings["reimported_export"]),
                "non_photo": total(self.findings["non_photo"]),
            },
        }
        storage["freeable_gb"]["sum"] = round(sum(storage["freeable_gb"].values()), 2)
        return {"photos_scanned": self.scanned, "photos_considered": self.considered, "storage": storage,
                "virtual_copies_skipped": self.virtual_copies, "counts": counts,
                "wrong_folder_by_level": dict(levels), "wrong_folder_by_target_year": dict(sorted(by_year.items())),
                **({"file_check_denied": self.file_check_denied} if self.file_check_denied else {}),
                **({"duplicate_files_unreadable": self.unreadable} if self.unreadable else {})}


def result_path():
    return config_dir() / "audit" / "latest.json"


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
    """One background run. ``fetch(offset, limit, refresh)`` returns an audit_page dict."""

    def __init__(self, fetch: Callable[[int, int, bool], dict[str, Any]], year: int | None,
                 check_files: bool) -> None:
        self.fetch = fetch
        self.analysis = Analysis(year, check_files)
        self.state: dict[str, Any] = {"status": "running", "phase": "reading catalog", "year": year,
                                      "check_files": check_files, "started": time.time(), "total": None,
                                      "read": 0}
        self.unsupported: set[str] = set()
        self.thread = threading.Thread(target=self._run, name="catalog-audit", daemon=True)

    def start(self) -> Job:
        self.thread.start()
        return self

    def _fetch(self, offset: int) -> dict[str, Any]:
        """One page; a slow or busy Lightroom gets two more tries before the run fails."""
        for attempt in range(3):
            try:
                return self.fetch(offset, PAGE, offset == 0)
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(RETRY_SECONDS)
        raise AssertionError("unreachable")

    def _run(self) -> None:
        try:
            offset = 0
            while True:
                page = self._fetch(offset)
                self.unsupported.update(page.get("unsupported") or [])
                for rec in page.get("photos") or []:
                    self.analysis.add(rec)
                self.state["total"] = int(page["total"])
                nxt = int(page["next_offset"])
                self.state["read"] = nxt
                if nxt <= offset or nxt >= int(page["total"]):
                    break
                offset = nxt
            self.state["phase"] = "checking duplicate candidates"

            def progress(done: int, total: int) -> None:
                self.state["fingerprints"] = f"{done}/{total}"

            self.analysis.finish(progress)
            finished = time.time()
            result = {
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(finished)),
                "seconds": round(finished - self.state["started"]),
                "year": self.state["year"], "check_files": self.state["check_files"],
                "unsupported_keys": sorted(self.unsupported),
                "summary": self.analysis.summary(), "findings": self.analysis.findings,
            }
            save_result(result)
            self.state.update(status="done", phase="done", seconds=result["seconds"])
        except Exception as exc:  # noqa: BLE001 - reported through catalog_audit_status
            self.state.update(status="failed", error=f"{type(exc).__name__}: {exc}")

    def status(self) -> dict[str, Any]:
        out = {k: v for k, v in self.state.items() if k != "started"}
        elapsed = time.time() - self.state["started"]
        out["elapsed_seconds"] = round(elapsed)
        total, read = self.state.get("total"), self.state.get("read") or 0
        if self.state["status"] == "running" and total and read and self.state["phase"] == "reading catalog":
            out["percent"] = round(100 * read / total, 1)
            out["eta_seconds"] = round(elapsed * (total - read) / read)
        return out


_lock = threading.Lock()
_job: Job | None = None


def start(fetch: Callable[[int, int, bool], dict[str, Any]], year: int | None, check_files: bool) -> tuple[bool, Job]:
    """Start a run unless one is running. Returns (started, job)."""
    global _job
    with _lock:
        if _job is not None and _job.state["status"] == "running":
            return False, _job
        _job = Job(fetch, year, check_files).start()
        return True, _job


def current() -> Job | None:
    return _job


# ---------------------------------------------------------------- review collections
ROOT_SET = "Prüfen"
MAX_GROUPS_PER_DAY_MODE = 300
SINGLE = {
    "non_photo": "keine Kamera-Fotos",
    "reimported_export": "Export wieder importiert",
    "no_date": "ohne Aufnahmedatum",
    "implausible_date": "Datum unplausibel",
    "unrecognised_folder": "kein Datumsordner",
    "missing_file": "Datei fehlt",
}


def review_groups(result: dict[str, Any], kind: str, year: int | None = None) -> tuple[list[dict[str, Any]], str]:
    """Collections to create for one kind: [{set, name, photo_ids}], plus how they are cut."""
    findings = result["findings"]
    if kind == "wrong_folder":
        items = [f for f in findings["wrong_folder"] if not year or f["expected"][:4] == str(year)]
        by_day: dict[str, list[str]] = defaultdict(list)
        for f in items:
            by_day[f["expected"]].append(f["id"])
        if len(by_day) <= MAX_GROUPS_PER_DAY_MODE:
            groups = {f"→ {d}": ids for d, ids in by_day.items()}
            mode = "one collection per target day folder"
        else:
            by_month: dict[str, list[str]] = defaultdict(list)
            for d, ids in by_day.items():
                by_month[d[:7]] += ids
            groups = {f"→ {m}": ids for m, ids in by_month.items()}
            mode = "one collection per target month (sort by capture time, drag day by day)"
        return [{"set": "Ordner falsch", "name": n, "photo_ids": ids} for n, ids in sorted(groups.items())], mode
    if kind == "duplicates":
        ids = [r["id"] for g in findings["duplicates"] for r in g["remove"]]
        keep = [g["keep"]["id"] for g in findings["duplicates"]]
        return [{"set": "Duplikate", "name": "entfernen", "photo_ids": ids},
                {"set": "Duplikate", "name": "behalten (Gegenstück)", "photo_ids": keep}], "remove + keep"
    if kind == "possible_duplicates":
        ids = [i for g in findings["possible_duplicates"] for i in [g["keep"]["id"]] + [r["id"] for r in g["remove"]]]
        return [{"set": "Duplikate", "name": "möglich – bitte ansehen", "photo_ids": ids}], "one collection"
    if kind == "jpeg_with_raw":
        items = findings.get(kind, [])
        return [{"set": "Duplikate", "name": "JPEG neben RAW – entfernen",
                 "photo_ids": [f["id"] for f in items if not f.get("check")]},
                {"set": "Duplikate", "name": "JPEG neben RAW – erst prüfen",
                 "photo_ids": [f["id"] for f in items if f.get("check")]}], "remove + check"
    if kind in SINGLE:
        return [{"set": "Sonstiges", "name": SINGLE[kind],
                 "photo_ids": [f["id"] for f in findings.get(kind, [])]}], "one collection"
    raise ValueError(f"unknown kind {kind}")


def chunk_groups(groups: list[dict[str, Any]], max_photos: int = 5000, max_groups: int = 300) -> list[list[dict[str, Any]]]:
    """Split into plugin calls; a big collection is filled over several calls (same name)."""
    calls: list[list[dict[str, Any]]] = [[]]
    used = 0
    for g in groups:
        ids = g["photo_ids"]
        for start in range(0, max(len(ids), 1), max_photos):
            part = ids[start:start + max_photos]
            if used + len(part) > max_photos or len(calls[-1]) >= max_groups:
                calls.append([])
                used = 0
            calls[-1].append({**g, "photo_ids": part})
            used += len(part)
    return [c for c in calls if c]

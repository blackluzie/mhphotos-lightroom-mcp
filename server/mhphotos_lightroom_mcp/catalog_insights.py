"""Insights from the last catalog check: year overview, folder moves, archive candidates, gear, spots.

Everything here works on the files the catalog check leaves on the Mac (``audit/latest.json`` with the
findings and ``audit/snapshot.json.gz`` with one compact row per photo), so no Lightroom call is needed and the
answers come at once even for 250k+ photos. Read only.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Any

from .catalog_audit import folder_dates

GB = 1024 ** 3
OVERVIEW_KINDS = ("wrong_folder", "no_date", "date_in_name", "video_no_date", "implausible_date", "unrecognised_folder",
                  "missing_file", "non_photo", "jpeg_with_raw", "reimported_export", "duplicates", "near_duplicates",
                  "possible_duplicates")


def _gb(n: float) -> float:
    return round(n / GB, 2)


def _year_of(item: dict[str, Any], kind: str) -> str:
    if kind == "wrong_folder":
        return str(item.get("expected") or "")[:4] or "ohne Datum"
    if kind == "date_in_name":
        return str(item.get("name_date") or "")[:4] or "ohne Datum"
    return str(item.get("captured") or "")[:4] or "ohne Datum"


def photo_year(p: dict[str, Any]) -> str:
    """Capture year, else the year folder, else "ohne Datum"."""
    return str(p.get("t") or "")[:4] or (folder_dates((p.get("folder") or "") + "/x")[0] or "ohne Datum")


# ---------------------------------------------------------------- 2: overview per year, folder moves
def overview(result: dict[str, Any], photos: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Findings of the last check per year (counts, storage) plus the totals."""
    rows: dict[str, dict[str, Any]] = defaultdict(lambda: defaultdict(int))
    findings = result.get("findings") or {}
    for kind in OVERVIEW_KINDS:
        for item in findings.get(kind) or []:
            year = _year_of(item, kind)
            if kind in ("duplicates", "near_duplicates"):
                rows[year][f"{kind}_to_remove"] += len(item.get("remove") or [])
            elif kind == "possible_duplicates":
                rows[year][kind] += 1 + len(item.get("remove") or [])
            else:
                rows[year][kind] += 1
    by_year_gb = (result.get("summary") or {}).get("storage", {}).get("by_year_gb", {})
    counts: Counter[str] = Counter(photo_year(p) for p in photos or [])
    years = sorted(set(rows) | set(by_year_gb) | set(counts), reverse=True)
    table = []
    for y in years:
        row = {"year": y, **({"photos": counts[y]} if counts else {}), **({"gb": by_year_gb[y]} if y in by_year_gb else {}),
               **dict(rows.get(y, {}))}
        row["open_points"] = sum(v for k, v in row.items() if k not in ("year", "photos", "gb"))
        table.append(row)
    return {"checked_at": result.get("finished_at"), "scope": result.get("year") or "whole catalog",
            "years": table, "totals": (result.get("summary") or {}).get("counts", {})}


def folder_moves(result: dict[str, Any], photos: list[dict[str, Any]] | None, year: int | None = None,
                 level: str | None = None) -> list[dict[str, Any]]:
    """wrong_folder findings per source folder: where its photos belong and whether the whole folder moves.

    whole_folder=True: every photo of the folder belongs into the same target, so the folder can be dragged (or
    renamed) in Lightroom's Folders panel in one step instead of photo by photo.
    """
    full = {p["id"]: p.get("folder") for p in photos or [] if p.get("id")}
    in_folder: Counter[str] = Counter(p.get("folder") for p in photos or [] if p.get("folder"))
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    levels: dict[str, Counter[str]] = defaultdict(Counter)
    for f in (result.get("findings") or {}).get("wrong_folder") or []:
        if year and str(f.get("expected") or "")[:4] != str(year):
            continue
        if level and f.get("level") != level:
            continue
        src = full.get(f["id"]) or f.get("folder") or "?"
        groups[src][f.get("expected_path") or f.get("expected")] += 1
        levels[src][f.get("level") or "?"] += 1
    out = []
    for src, targets in groups.items():
        moving = sum(targets.values())
        total = in_folder.get(src) or None
        item: dict[str, Any] = {"folder": src, "photos_to_move": moving, "targets": dict(targets.most_common(5)),
                                "levels": dict(levels[src])}
        if total:
            item["photos_in_folder"] = total
            item["whole_folder"] = len(targets) == 1 and moving == total
        if len(targets) > 5:
            item["more_targets"] = len(targets) - 5
        out.append(item)
    out.sort(key=lambda i: (-i["photos_to_move"], i["folder"]))
    return out


# ---------------------------------------------------------------- 3: archive candidates
def untouched(p: dict[str, Any]) -> bool:
    """No stars, no pick flag, no keywords, no edit, no colour label, no virtual copies."""
    return not (p.get("r") or p.get("pick") == 1 or p.get("kw") or p.get("edited") or p.get("label") or p.get("copies"))


def archive_candidates(photos: list[dict[str, Any]], min_age_years: int = 5, today: str | None = None,
                       include_videos: bool = True) -> dict[str, Any]:
    """Untouched photos older than min_age_years, per folder and year, plus rejected photos of any age."""
    this_year = int((today or date.today().isoformat())[:4])
    limit = this_year - min_age_years
    folders: dict[str, dict[str, Any]] = {}
    by_year: dict[str, dict[str, Any]] = defaultdict(lambda: {"photos": 0, "bytes": 0, "ids": []})
    rejected = {"photos": 0, "bytes": 0, "ids": []}
    for p in photos:
        size = int(p.get("size") or 0)
        folder = p.get("folder") or "?"
        f = folders.setdefault(folder, {"folder": folder, "photos_in_folder": 0, "candidates": 0, "bytes": 0,
                                        "years": Counter()})
        f["photos_in_folder"] += 1
        if p.get("pick") == -1:
            rejected["photos"] += 1
            rejected["bytes"] += size
            rejected["ids"].append(p["id"])
        if not include_videos and (p.get("fmt") or "").upper() == "VIDEO":
            continue
        year = photo_year(p)
        if not year.isdigit() or int(year) > limit or not untouched(p):
            continue
        f["candidates"] += 1
        f["bytes"] += size
        f["years"][year] += 1
        y = by_year[year]
        y["photos"] += 1
        y["bytes"] += size
        y["ids"].append(p["id"])
    listed = []
    for f in folders.values():
        if not f["candidates"]:
            continue
        listed.append({"folder": f["folder"], "candidates": f["candidates"], "photos_in_folder": f["photos_in_folder"],
                       "gb": _gb(f["bytes"]), "whole_folder": f["candidates"] == f["photos_in_folder"],
                       "year": f["years"].most_common(1)[0][0]})
    listed.sort(key=lambda i: (-i["gb"], i["folder"]))
    total_bytes = sum(y["bytes"] for y in by_year.values())
    return {
        "rule": f"no stars, flag, keywords, edits, colour label or virtual copies; captured {limit} or earlier",
        "candidates": sum(y["photos"] for y in by_year.values()), "gb": _gb(total_bytes),
        "whole_folders": sum(1 for f in listed if f["whole_folder"]),
        "whole_folders_gb": round(sum(f["gb"] for f in listed if f["whole_folder"]), 2),
        "by_year": {y: {"photos": v["photos"], "gb": _gb(v["bytes"])} for y, v in sorted(by_year.items())},
        "rejected_any_age": {"photos": rejected["photos"], "gb": _gb(rejected["bytes"])},
        "folders": listed,
        "_ids_by_year": {y: v["ids"] for y, v in by_year.items()},
        "_rejected_ids": rejected["ids"],
    }


# ---------------------------------------------------------------- 7: gear statistics
def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _focal_bucket(fl: int) -> str:
    return f"{fl} mm"


def gear_usage(photos: list[dict[str, Any]], year: int | None = None) -> dict[str, Any]:
    """Photos per camera and per lens: how many, how many rated 4+ and 5, first and last use, focal lengths."""
    def blank() -> dict[str, Any]:
        return {"photos": 0, "rated_4_plus": 0, "five_star": 0, "first": None, "last": None,
                "by_year": Counter(), "focal": Counter()}

    cams: dict[str, dict[str, Any]] = defaultdict(blank)
    lenses: dict[str, dict[str, Any]] = defaultdict(blank)
    considered = 0
    for p in photos:
        t = str(p.get("t") or "")
        if year and t[:4] != str(year):
            continue
        considered += 1
        rating = int(p.get("r") or 0)
        for key, table in ((p.get("cam"), cams), (p.get("lens"), lenses)):
            if not key:
                continue
            s = table[key]
            s["photos"] += 1
            s["rated_4_plus"] += rating >= 4
            s["five_star"] += rating == 5
            if t:
                day = t[:10]
                s["first"] = day if not s["first"] or day < s["first"] else s["first"]
                s["last"] = day if not s["last"] or day > s["last"] else s["last"]
                s["by_year"][t[:4]] += 1
            if p.get("fl"):
                s["focal"][_focal_bucket(int(p["fl"]))] += 1

    def finish(table: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for name, s in table.items():
            out.append({"name": name, "photos": s["photos"], "rated_4_plus": s["rated_4_plus"],
                        "five_star": s["five_star"], "first_used": s["first"], "last_used": s["last"],
                        "by_year": dict(sorted(s["by_year"].items())),
                        "top_focal_lengths": dict(s["focal"].most_common(5))})
        out.sort(key=lambda i: -i["photos"])
        return out

    return {"year": year, "photos_considered": considered, "cameras": finish(cams), "lenses": finish(lenses)}


_CATEGORY = {"kamera": "camera", "objektiv": "lens", "camera": "camera", "lens": "lens"}


def gear_category(item: dict[str, Any]) -> str | None:
    """camera / lens / None; the MHPhotos app stores "kamera"/"objektiv" (sharedCategory "camera"/"lens")."""
    return _CATEGORY.get(str(item.get("sharedCategory") or item.get("category") or "").lower())


def _gear_match(name: str, gear: list[dict[str, Any]], category: str) -> dict[str, Any] | None:
    """Best gear item for an EXIF camera/lens name: the model (or brand + model) inside the name, or vice versa."""
    n = _norm(name)
    best, best_len = None, 0
    for item in gear:
        if gear_category(item) != category:
            continue
        for cand in {_norm(item.get("model")), _norm(f"{item.get('brand', '')}{item.get('model', '')}"),
                     _norm(item.get("name"))}:
            if len(cand) < 3:
                continue
            if (cand in n or n in cand) and len(cand) > best_len:
                best, best_len = item, len(cand)
    return best


def match_gear(usage: dict[str, Any], gear: list[dict[str, Any]]) -> dict[str, Any]:
    """Link camera/lens statistics to the gear list of the MHPhotos site."""
    items: dict[str, dict[str, Any]] = {}
    unmatched: dict[str, list[dict[str, Any]]] = {"cameras": [], "lenses": []}
    for key, category in (("cameras", "camera"), ("lenses", "lens")):
        for s in usage[key]:
            hit = _gear_match(s["name"], gear, category)
            if hit is None:
                unmatched[key].append({"name": s["name"], "photos": s["photos"]})
                continue
            cur = items.setdefault(hit["id"], {"gear_id": hit["id"], "gear_name": hit.get("name"), "category": category,
                                               "exif_names": [], "photos": 0, "rated_4_plus": 0, "five_star": 0,
                                               "first_used": None, "last_used": None, "top_focal_lengths": Counter()})
            cur["exif_names"].append(s["name"])
            for k in ("photos", "rated_4_plus", "five_star"):
                cur[k] += s[k]
            for k, pick in (("first_used", min), ("last_used", max)):
                vals = [v for v in (cur[k], s[k]) if v]
                cur[k] = pick(vals) if vals else None
            cur["top_focal_lengths"].update(s["top_focal_lengths"])
    for cur in items.values():
        cur["top_focal_lengths"] = dict(cur["top_focal_lengths"].most_common(5))
    used = set(items)
    unused = [{"gear_id": g["id"], "name": g.get("name"), "category": gear_category(g)} for g in gear
              if gear_category(g) and (g.get("status") or "active") == "active" and g["id"] not in used]
    return {"matched": sorted(items.values(), key=lambda i: -i["photos"]), "unmatched": unmatched,
            "active_gear_without_photos": unused}


# ---------------------------------------------------------------- 6: spots and own photos
def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _spot_point(spot: dict[str, Any]) -> tuple[float, float] | None:
    for la, lo in (("lat", "lon"), ("lat", "lng"), ("latitude", "longitude")):
        lat, lon = spot.get(la), spot.get(lo)
        if isinstance(lat, int | float) and isinstance(lon, int | float) and (lat or lon):
            return float(lat), float(lon)
    return None


CELL = 0.01  # degrees


def spot_matches(photos: list[dict[str, Any]], spots: list[dict[str, Any]], radius_m: int = 300,
                 min_rating: int = 4, best: int = 5) -> dict[str, Any]:
    """Own photos within radius_m of each spot: counts, best photos, and spots without a good own photo."""
    grid: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    with_gps = 0
    for p in photos:
        lat, lon = p.get("lat"), p.get("lon")
        if isinstance(lat, int | float) and isinstance(lon, int | float):
            with_gps += 1
            grid[(math.floor(lat / CELL), math.floor(lon / CELL))].append(p)
    found, missing, skipped = [], [], 0
    for spot in spots:
        point = _spot_point(spot)
        if point is None or not spot.get("id"):
            skipped += 1
            continue
        lat, lon = point
        dy = math.ceil(radius_m / (CELL * 111_000)) + 1
        dx = math.ceil(radius_m / (CELL * 111_000 * max(0.1, math.cos(math.radians(lat))))) + 1
        cy, cx = math.floor(lat / CELL), math.floor(lon / CELL)
        near = []
        for y in range(cy - dy, cy + dy + 1):
            for x in range(cx - dx, cx + dx + 1):
                for p in grid.get((y, x), ()):
                    if _distance_m(lat, lon, p["lat"], p["lon"]) <= radius_m:
                        near.append(p)
        rated = [p for p in near if int(p.get("r") or 0) >= min_rating]
        entry = {"spot_id": spot["id"], "name": spot.get("name") or spot.get("title") or spot["id"],
                 "photos": len(near), "rated": len(rated)}
        if near:
            ranked = sorted(near, key=lambda p: (int(p.get("r") or 0), str(p.get("t") or "")), reverse=True)
            entry["best_rating"] = int(ranked[0].get("r") or 0)
            entry["last_captured"] = max((str(p.get("t") or "") for p in near), default="")[:10] or None
            entry["best"] = [{"id": p["id"], "file": p.get("file"), "rating": int(p.get("r") or 0),
                              "captured": p.get("t")} for p in ranked[:best]]
            found.append(entry)
        if not rated:
            missing.append({k: entry[k] for k in ("spot_id", "name", "photos")})
    found.sort(key=lambda e: (-e.get("best_rating", 0), -e["rated"], -e["photos"]))
    return {"radius_m": radius_m, "min_rating": min_rating, "photos_with_gps": with_gps, "spots": len(spots),
            "spots_without_position": skipped, "spots_with_own_photos": found,
            "spots_without_good_own_photo": missing}


# ---------------------------------------------------------------- duplicate imports per folder
def duplicate_folders(result: dict[str, Any], photos: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Verified duplicates of the last check per folder pair: a folder whose photos are copies of another folder
    (a card imported twice, a backup imported again). whole_folder=True: every photo of the folder is a copy, so
    the folder can go as a whole after a look in Lightroom."""
    full = {p["id"]: p.get("folder") for p in photos or [] if p.get("id")}
    in_folder: Counter[str] = Counter(p.get("folder") for p in photos or [] if p.get("folder"))
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for g in (result.get("findings") or {}).get("duplicates") or []:
        keep = g.get("keep") or {}
        kf = full.get(keep.get("id")) or keep.get("folder") or ""
        for r in g.get("remove") or []:
            rf = full.get(r.get("id")) or r.get("folder") or ""
            row = pairs.setdefault((rf, kf), {"copy_folder": rf, "original_folder": kf, "photos": 0, "bytes": 0,
                                              "photo_ids": []})
            row["photos"] += 1
            row["bytes"] += int(r.get("bytes") or 0)
            row["photo_ids"].append(r["id"])
    out = []
    for row in pairs.values():
        total = in_folder.get(row["copy_folder"])
        row["gb"] = _gb(row.pop("bytes"))
        if total:
            row["photos_in_folder"] = total
            row["whole_folder"] = row["photos"] >= total
        row["same_folder"] = row["copy_folder"] == row["original_folder"]
        out.append(row)
    out.sort(key=lambda r: (-r["photos"], r["copy_folder"]))
    return out


# ---------------------------------------------------------------- missing and offline files
def volume_of(folder: str) -> str:
    """'/Volumes/NAS/Fotos/2020' -> '/Volumes/NAS'; other paths belong to the Mac's own disk ('/')."""
    parts = PurePosixPath(folder or "/").parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return f"/Volumes/{parts[2]}"
    if len(parts) >= 3 and parts[1] == "mnt":  # Linux test machines
        return f"/mnt/{parts[2]}"
    return "/"


def missing_by_drive(result: dict[str, Any], photos: list[dict[str, Any]] | None,
                     is_dir: Callable[[str], bool]) -> dict[str, Any]:
    """missing_file findings per drive and folder. A drive that is not mounted is offline (connect it, nothing
    is lost); on a mounted drive the files are really gone or were moved outside Lightroom."""
    full = {p["id"]: p.get("folder") for p in photos or [] if p.get("id")}
    in_folder: Counter[str] = Counter(p.get("folder") for p in photos or [] if p.get("folder"))
    drives: dict[str, dict[str, Any]] = {}
    for f in (result.get("findings") or {}).get("missing_file") or []:
        folder = full.get(f.get("id")) or f.get("folder") or ""
        vol = volume_of(folder)
        d = drives.setdefault(vol, {"drive": vol, "online": is_dir(vol), "photos": 0, "folders": Counter(),
                                    "photo_ids": []})
        d["photos"] += 1
        d["folders"][folder] += 1
        d["photo_ids"].append(f["id"])
    out = []
    for d in drives.values():
        folders = [{"folder": k, "missing": n, **({"photos_in_folder": in_folder[k], "whole_folder": n >= in_folder[k]}
                                                  if in_folder.get(k) else {})}
                   for k, n in d.pop("folders").most_common()]
        d["status"] = "missing" if d["online"] else "offline"
        d["hint"] = ("Laufwerk ist angeschlossen: Dateien fehlen oder wurden außerhalb von Lightroom verschoben "
                     "(Lightroom: Ordner suchen / fehlende Fotos suchen)." if d["online"] else
                     "Laufwerk ist nicht angeschlossen: anschließen, dann ist nichts verloren.")
        d["folder_count"] = len(folders)
        d["folders"] = folders
        out.append(d)
    out.sort(key=lambda d: (d["online"], -d["photos"]))
    return {"checked_files": bool(result.get("check_files")), "drives": out,
            "missing_total": sum(d["photos"] for d in out)}


# ---------------------------------------------------------------- GPS gaps
def _ts(p: dict[str, Any]) -> float | None:
    try:
        return datetime.fromisoformat(str(p.get("t") or "")[:19]).timestamp()
    except ValueError:
        return None


def gps_gaps(photos: list[dict[str, Any]], window_minutes: int = 30, captured_from: str | None = None,
             captured_to: str | None = None) -> dict[str, Any]:
    """Photos without GPS that were taken at most window_minutes before or after a photo with GPS (any camera,
    e.g. the phone) get that photo's place as a suggestion. Videos are left out."""
    def in_range(p: dict[str, Any]) -> bool:
        day = str(p.get("t") or "")[:10]
        return bool(day) and (not captured_from or day >= captured_from) and (not captured_to or day <= captured_to)

    with_gps = sorted(((t, p) for p in photos if p.get("lat") is not None and p.get("lon") is not None
                       and (t := _ts(p)) is not None), key=lambda x: x[0])
    times = [t for t, _ in with_gps]
    window = window_minutes * 60
    suggestions, without = [], 0
    for p in photos:
        if p.get("lat") is not None or not in_range(p) or (p.get("fmt") or "").upper() == "VIDEO":
            continue
        without += 1
        t = _ts(p)
        if t is None or not times:
            continue
        i = bisect_left(times, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(times) and abs(times[j] - t) <= window and (best is None or abs(times[j] - t) < abs(times[best] - t)):
                best = j
        if best is None:
            continue
        src = with_gps[best][1]
        suggestions.append({"id": p["id"], "file": p.get("file"), "t": p.get("t"), "cam": p.get("cam"),
                            "lat": src["lat"], "lon": src["lon"], "from_id": src["id"], "from_file": src.get("file"),
                            "from_cam": src.get("cam"), "minutes_apart": round(abs(times[best] - t) / 60, 1)})
    by_month: Counter[str] = Counter(str(s["t"])[:7] for s in suggestions)
    return {"without_gps": without, "suggestions_total": len(suggestions), "by_month": dict(sorted(by_month.items())),
            "suggestions": suggestions}


def gps_groups(suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One set_location call per coordinate."""
    groups: dict[tuple[float, float], list[str]] = defaultdict(list)
    for s in suggestions:
        groups[(s["lat"], s["lon"])].append(s["id"])
    return [{"lat": lat, "lon": lon, "photo_ids": ids} for (lat, lon), ids in groups.items()]


# ---------------------------------------------------------------- print candidates
# common wall-art sizes (long edge in cm) and the pixels they need at 150 dpi, enough for viewing distance
PRINT_SIZES = ((120, "120×80"), (90, "90×60"), (60, "60×40"), (45, "45×30"), (30, "30×20"))
PRINT_DPI = 150


def edge_px(p: dict[str, Any]) -> tuple[int, int]:
    """(long, short) edge after cropping, else of the original."""
    w, h = p.get("cw") or p.get("w") or 0, p.get("ch") or p.get("h") or 0
    return (max(w, h), min(w, h))


def max_print(p: dict[str, Any], dpi: int = PRINT_DPI) -> str | None:
    long_edge = edge_px(p)[0]
    for cm, label in PRINT_SIZES:
        if long_edge >= cm / 2.54 * dpi:
            return label
    return None


def _score(p: dict[str, Any]) -> float:
    return (p.get("r") or 0) * 10 + (2 if p.get("pick") == 1 else 0) + (1 if p.get("edited") else 0) + \
        min(p.get("kw") or 0, 5) * 0.2


def print_candidates(photos: list[dict[str, Any]], min_rating: int = 4, min_size: str = "45×30",
                     dpi: int = PRINT_DPI) -> dict[str, Any]:
    """Rated photos (min_rating+, not rejected, no videos) with enough pixels after cropping for a wall print of
    at least min_size, grouped by the biggest size they allow."""
    labels = [label for _, label in PRINT_SIZES]
    if min_size not in labels:
        raise ValueError(f"min_size must be one of {', '.join(labels)}")
    allowed = labels[: labels.index(min_size) + 1]
    by_size: dict[str, list[dict[str, Any]]] = defaultdict(list)
    too_small = 0
    for p in photos:
        if (p.get("r") or 0) < min_rating or p.get("pick") == -1 or (p.get("fmt") or "").upper() == "VIDEO":
            continue
        size = max_print(p, dpi)
        if size not in allowed:
            too_small += 1
            continue
        long_edge, short_edge = edge_px(p)
        by_size[size].append({"id": p["id"], "file": p.get("file"), "t": p.get("t"), "r": p.get("r"),
                              "px": f"{long_edge}×{short_edge}", "cropped": bool(p.get("cw")),
                              "orientation": _orientation(p), "_score": _score(p)})
    groups = []
    for label in allowed:
        items = sorted(by_size.get(label, []), key=lambda x: (-x["_score"], str(x["t"])))
        for x in items:
            x.pop("_score")
        groups.append({"max_size": label, "photos": len(items), "items": items})
    return {"min_rating": min_rating, "dpi": dpi, "too_small": too_small,
            "candidates": sum(g["photos"] for g in groups), "groups": groups}


def _orientation(p: dict[str, Any]) -> str:
    w, h = p.get("cw") or p.get("w") or 0, p.get("ch") or p.get("h") or 0
    if not w or not h:
        return "unknown"
    return "landscape" if w > h * 1.05 else "portrait" if h > w * 1.05 else "square"


# ---------------------------------------------------------------- photo calendar
MONTHS_DE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
             "November", "Dezember")


def calendar_candidates(photos: list[dict[str, Any]], per_month: int = 8, min_rating: int = 4,
                        orientation: str = "landscape", min_size: str = "45×30",
                        exclude_ids: set[str] | None = None) -> dict[str, Any]:
    """Suggestions for a 12-month photo calendar: per month the best photos taken in that month (any year), with
    variety (at most one photo per day, then spread over years and places). orientation: landscape, portrait
    or any. min_size: the print must allow at least this size (A3 calendar ≈ 45×30)."""
    labels = [label for _, label in PRINT_SIZES]
    if min_size not in labels:
        raise ValueError(f"min_size must be one of {', '.join(labels)}")
    if orientation not in ("landscape", "portrait", "any"):
        raise ValueError("orientation must be landscape, portrait or any")
    allowed = set(labels[: labels.index(min_size) + 1])
    exclude = exclude_ids or set()
    pool: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for p in photos:
        t = str(p.get("t") or "")
        if len(t) < 10 or p["id"] in exclude or (p.get("r") or 0) < min_rating or p.get("pick") == -1:
            continue
        if (p.get("fmt") or "").upper() == "VIDEO" or max_print(p) not in allowed:
            continue
        if orientation != "any" and _orientation(p) != orientation:
            continue
        pool[int(t[5:7])].append(p)
    months = []
    for m in range(1, 13):
        ranked = sorted(pool.get(m, []), key=lambda p: (-_score(p), str(p.get("t"))))
        chosen: list[dict[str, Any]] = []
        days: set[str] = set()
        years: Counter[str] = Counter()
        places: Counter[tuple[int, int]] = Counter()
        # first pass: one per day, at most two per year and per place (~10 km); second pass fills up per day only
        for strict in (True, False):
            for p in ranked:
                if len(chosen) >= per_month:
                    break
                day, year = str(p["t"])[:10], str(p["t"])[:4]
                place = (round(p["lat"] * 10), round(p["lon"] * 10)) if p.get("lat") is not None else None
                if day in days or p in chosen:
                    continue
                if strict and (years[year] >= 2 or (place and places[place] >= 2)):
                    continue
                chosen.append(p)
                days.add(day)
                years[year] += 1
                if place:
                    places[place] += 1
        months.append({"month": m, "name": MONTHS_DE[m - 1], "pool": len(ranked),
                       "suggestions": [{"id": p["id"], "file": p.get("file"), "t": p.get("t"), "r": p.get("r"),
                                        "max_size": max_print(p), "place": [p.get("lat"), p.get("lon")]
                                        if p.get("lat") is not None else None} for p in chosen]})
    return {"per_month": per_month, "min_rating": min_rating, "orientation": orientation, "min_size": min_size,
            "months": months, "months_short": [m["name"] for m in months if len(m["suggestions"]) < per_month]}

"""Which Lightroom photos are already online (mhphotos.de, Instagram)?

The MHPhotos Foto-Inbox keeps, per uploaded photo, the original file name, the capture time, the camera and
(since this version) the Lightroom id; an item with status "uebernommen" is on the homepage, an item with an
instagramId belongs to an Instagram post. This module matches those items to Lightroom photos (capture-date
records from photos_by_date), so the photos can get the keywords "MHPhotos-Online" / "Instagram".
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import PurePosixPath
from typing import Any

ONLINE_KEYWORD = "MHPhotos-Online"
INSTAGRAM_KEYWORD = "Instagram"
SMART_PARENT = "MHPhotos"
SMART_COLLECTIONS = {ONLINE_KEYWORD: "Online auf mhphotos.de", INSTAGRAM_KEYWORD: "Auf Instagram"}
# Copy suffixes: "-2", " (3)", "-Bearbeitet", and the long number some exports append ("IMG_8493-113706907").
_COPY = re.compile(r"(?:[ _-]\d{1,2}|[ _-]\d{7,}|\s*\(\d+\)|[ _-]?(?:kopie|copy|edit|bearbeitet))$")


def file_stem(name: str | None) -> str:
    """'IMG_1234.DNG' -> 'img_1234'."""
    return PurePosixPath(str(name or "")).stem.lower().strip()


def stem_variants(stem: str) -> set[str]:
    """An export may carry a copy suffix: 'img_1234-2' also stands for 'img_1234' (but 'img_1234' not for 'img')."""
    return {stem, _COPY.sub("", stem)} - {""}


def wanted(item: dict[str, Any]) -> list[str]:
    """Keywords an inbox item stands for: on the homepage and/or on Instagram."""
    if item.get("deletedAt") or item.get("status") == "geloescht":
        return []
    out = []
    if item.get("status") == "uebernommen" or item.get("homepagePhotoId"):
        out.append(ONLINE_KEYWORD)
    if item.get("instagramId"):
        out.append(INSTAGRAM_KEYWORD)
    return out


def capture_second(item: dict[str, Any]) -> str | None:
    """'2026-10-19T14:03:22.000Z' -> '2026-10-19T14:03:22' (the inbox keeps the camera's wall clock as UTC)."""
    value = str(item.get("captureDate") or "")
    return value[:19] if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value) else None


def days_needed(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Date ranges (one day either side of each capture day, merged) to read from Lightroom."""
    days: set[date] = set()
    for item in items:
        sec = capture_second(item)
        if sec:
            d = date.fromisoformat(sec[:10])
            days.update({d - timedelta(days=1), d, d + timedelta(days=1)})
    ranges: list[tuple[str, str]] = []
    for d in sorted(days):
        if ranges and date.fromisoformat(ranges[-1][1]) + timedelta(days=1) >= d:
            ranges[-1] = (ranges[-1][0], d.isoformat())
        else:
            ranges.append((d.isoformat(), d.isoformat()))
    return ranges


def match(items: list[dict[str, Any]], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Pair inbox items with Lightroom records. Best first: Lightroom id, same second + same file name,
    same file name within a day, same second + same camera.

    One shot often lives in several files (RAW + JPG, "-2" copies, "-Bearbeitet.tif"): every file with the same
    capture second and the same name (copy suffixes ignored) is matched, so each item gives one match per file.
    """
    by_id = {r["id"]: r for r in records if r.get("id")}
    by_second: dict[str, list[dict[str, Any]]] = {}
    by_stem: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        if r.get("t"):
            by_second.setdefault(str(r["t"])[:19], []).append(r)
        if r.get("file"):
            by_stem.setdefault(file_stem(r["file"]), []).append(r)

    def same_shot(sec: str | None, stem: str) -> list[dict[str, Any]]:
        names = stem_variants(stem)
        return [r for r in by_second.get(sec or "", []) if names & stem_variants(file_stem(r.get("file")))]

    matches, unmatched = [], []
    for item in items:
        keys = wanted(item)
        if not keys:
            continue
        sec, stem, cam = capture_second(item), file_stem(item.get("originalFileName")), (item.get("camera") or "").lower()
        hits: list[dict[str, Any]] = []
        how = None
        if item.get("lightroomId") in by_id:
            hit = by_id[item["lightroomId"]]
            hits, how = [hit], "lightroom id"
            if hit.get("t") and hit.get("file"):
                hits += [r for r in same_shot(str(hit["t"])[:19], file_stem(hit["file"])) if r["id"] != hit["id"]]
        if not hits and sec:
            named = same_shot(sec, stem) if stem else []
            if named:
                hits, how = named, "capture time + file name"
            else:
                cams = [r for r in by_second.get(sec, []) if cam and (r.get("cam") or "").lower() == cam]
                if len(cams) == 1:
                    hits, how = cams, "capture time + camera"
        if not hits and sec and stem:
            near = [r for v in stem_variants(stem) for r in by_stem.get(v, []) if r.get("t") and
                    abs((date.fromisoformat(str(r["t"])[:10]) - date.fromisoformat(sec[:10])).days) <= 1]
            if len(near) == 1:
                hits, how = near, "file name, capture day ±1"
        base = {"item_id": item.get("id"), "file": item.get("originalFileName"), "keywords": keys}
        if not hits:
            unmatched.append({**base, "captured": sec})
        for hit in hits:
            matches.append({**base, "photo_id": hit["id"], "lightroom_file": hit.get("file"), "how": how,
                            "already_instagram": bool(hit.get("insta"))})
    return {"matches": matches, "unmatched": unmatched}

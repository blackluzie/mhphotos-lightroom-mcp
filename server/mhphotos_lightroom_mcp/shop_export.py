"""Homepage photos -> folders named like the mhphotos.de galleries, for a print shop (Pictrs, Saal Photo Portal).

A homepage photo (manifest of mhphotos.de) is linked to its Lightroom original through the MHPhotos Foto-Inbox
item it came from (homepagePhotoId) and online_marks.match. Each gallery of the photo becomes a folder path
"<Bereich>/<Eltern-Galerie>/<Galerie>", e.g. "Unterwegs/Italien/Brixen". Pictrs' FTP autoimport turns such a
folder tree into galleries with sub-galleries; for Saal the folders are uploaded by hand.

A small state file next to the export remembers which photo went into which folder, so the next run only
exports what is new on the homepage.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SUBDIR = "Wandbilder"
STATE_FILE = ".exportiert.json"
AREA_TITLES = {"heimat": "Heimat", "unterwegs": "Unterwegs", "jahreszeiten": "Jahreszeiten", "malou": "Malou"}
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_name(name: str) -> str:
    """A folder name that works on macOS, Windows and FTP servers."""
    return _BAD.sub("-", str(name or "")).strip(" .-") or "Ohne Namen"


def folder_for(gallery_id: str, galleries: dict[str, dict[str, Any]]) -> str | None:
    """'unterwegs/italien/brixen' -> 'Unterwegs/Italien/Brixen' (titles of the area, parents and gallery)."""
    if gallery_id not in galleries:
        return None
    chain: list[str] = []
    current: str | None = gallery_id
    seen: set[str] = set()
    while current and current in galleries and current not in seen:
        seen.add(current)
        g = galleries[current]
        chain.append(safe_name(g.get("shortTitle") or g.get("title") or current.rsplit("/", 1)[-1]))
        current = g.get("parent") or ("/".join(current.split("/")[:-1]) if current.count("/") > 1 else None)
    area = galleries[gallery_id].get("area") or gallery_id.split("/", 1)[0]
    chain.append(safe_name(AREA_TITLES.get(area, area.title())))
    return "/".join(reversed(chain))


def lightroom_ids(matches: list[dict[str, Any]], homepage_of: dict[str, str]) -> dict[str, str]:
    """homepage photo id -> Lightroom photo id, from online_marks matches and item id -> homepagePhotoId."""
    out: dict[str, str] = {}
    for m in matches:
        hp = homepage_of.get(str(m.get("item_id")))
        if hp and m.get("photo_id"):
            out.setdefault(hp, m["photo_id"])
    return out


def plan(site_photos: list[dict[str, Any]], galleries: list[dict[str, Any]], lr_of: dict[str, str],
         done: dict[str, Any], gallery: str | None = None, again: bool = False) -> dict[str, Any]:
    """Which Lightroom photos go into which folder; photos that cannot be linked to Lightroom are listed."""
    by_id = {g["id"]: g for g in galleries if g.get("id")}
    folders: dict[str, list[str]] = {}
    already = 0
    not_in_lightroom: list[dict[str, Any]] = []
    unknown_galleries: set[str] = set()
    for photo in site_photos:
        cats = [c for c in photo.get("categories") or [] if not gallery or c == gallery or c.startswith(gallery + "/")]
        if not cats:
            continue
        lr = lr_of.get(str(photo.get("id")))
        if not lr:
            not_in_lightroom.append({"id": photo.get("id"), "title": photo.get("title"), "galleries": cats})
            continue
        for cat in cats:
            folder = folder_for(cat, by_id)
            if folder is None:
                unknown_galleries.add(cat)
                continue
            if not again and f"{lr}|{folder}" in done:
                already += 1
                continue
            if lr not in folders.setdefault(folder, []):
                folders[folder].append(lr)
    return {"folders": dict(sorted(folders.items())), "photos": sum(len(v) for v in folders.values()),
            "already_exported": already, "not_in_lightroom": not_in_lightroom,
            "unknown_galleries": sorted(unknown_galleries)}


def load_state(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(root: Path, done: dict[str, Any], folder: str, photo_ids: list[str]) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for pid in photo_ids:
        done[f"{pid}|{folder}"] = now
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(done, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(root / STATE_FILE)

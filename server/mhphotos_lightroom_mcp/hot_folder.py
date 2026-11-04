"""Hot folder into the verified export chain: Lightroom's auto-export -> n8n -> MHPhotos inbox.

The plugin's auto-export (one collection, e.g. "-> MHPhotos") drops JPEGs into a folder. This loop,
running inside the HTTP service, waits until a file has stopped growing, moves finished JPEGs into
a new export batch with checksums and a ready.json (exactly like export_photos) and tells n8n. n8n
then verifies and uploads them like any other export, so there is one upload path with one
duplicate check. Files that are not readable JPEGs move to "fehler/". Nothing is published.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

MAX_BATCH = 12
SETTLE_SECONDS = 10  # a file must be unchanged for this long (Lightroom writes in steps)
FAILED_DIR = "fehler"
JPEG_SUFFIXES = {".jpg", ".jpeg"}


def write_ready(batch: Path, files: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> None:
    """The ready marker the asset service lists; written last and atomically."""
    marker = batch / ".ready.tmp"
    marker.write_text(json.dumps({"files": [
        {**item, "name": Path(item["path"]).name, **(extra or {})} for item in files
    ]}))
    marker.chmod(0o600)
    marker.replace(batch / "ready.json")


def describe(path: Path) -> dict[str, Any]:
    """Checksum and size of a JPEG; raises ValueError for anything else."""
    with Image.open(path) as image:
        if image.format != "JPEG":
            raise ValueError("not a JPEG")
        pixels = image.size
        image.verify()
    data = path.read_bytes()
    return {"path": str(path), "bytes": len(data), "width": pixels[0], "height": pixels[1],
            "sha256": hashlib.sha256(data).hexdigest()}


class HotFolder:
    def __init__(self, folder: Path, export_root: Path,
                 notify: Callable[[str, list[dict[str, Any]]], bool | None],
                 clock: Callable[[], float] = time.time) -> None:
        self.folder = folder
        self.export_root = export_root
        self.notify = notify
        self.clock = clock
        self._seen: dict[Path, tuple[int, int]] = {}

    def _ready_files(self) -> list[Path]:
        """JPEG-like files that did not change since the previous scan and are old enough."""
        now, current, ready = self.clock(), {}, []
        for path in sorted(self.folder.iterdir()):
            if path.name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
            current[path] = (info.st_size, info.st_mtime_ns)
            if self._seen.get(path) == current[path] and now - info.st_mtime >= SETTLE_SECONDS:
                ready.append(path)
        self._seen = current
        return ready

    def _reject(self, path: Path, reason: str) -> None:
        failed = self.folder / FAILED_DIR
        failed.mkdir(exist_ok=True)
        shutil.move(str(path), failed / path.name)
        print(f"hot folder: {path.name} -> {FAILED_DIR}/ ({reason})", flush=True)

    def scan_once(self) -> dict[str, int]:
        if not self.folder.is_dir():
            return {"batches": 0, "files": 0, "rejected": 0}
        batches = moved = rejected = 0
        ready = self._ready_files()
        good = []
        for path in ready:
            if path.suffix.lower() not in JPEG_SUFFIXES:
                self._reject(path, "not a .jpg")
                rejected += 1
                continue
            try:
                describe(path)
            except (OSError, ValueError) as exc:
                self._reject(path, str(exc))
                rejected += 1
                continue
            good.append(path)
        for start in range(0, len(good), MAX_BATCH):
            chunk = good[start : start + MAX_BATCH]
            self.export_root.mkdir(parents=True, exist_ok=True)
            batch = self.export_root / ("export-" + uuid.uuid4().hex)
            batch.mkdir(mode=0o700)
            files = []
            for path in chunk:
                target = batch / path.name
                shutil.move(str(path), target)
                files.append(describe(target))
                self._seen.pop(path, None)
            write_ready(batch, files)
            moved += len(files)
            batches += 1
            notified = self.notify(batch.name, files)
            print(f"hot folder: {len(files)} JPEG(s) -> {batch.name}, n8n notified: {notified}", flush=True)
        return {"batches": batches, "files": moved, "rejected": rejected}

    def run(self, stop: threading.Event, interval: float = 15.0) -> None:
        print(f"hot folder: watching {self.folder}", flush=True)
        while not stop.wait(interval):
            try:
                self.scan_once()
            except Exception as exc:  # noqa: BLE001 - keep watching; e.g. a missing permission
                print(f"hot folder error: {exc}", flush=True)

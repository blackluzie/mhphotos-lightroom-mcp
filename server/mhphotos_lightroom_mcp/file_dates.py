"""Write a capture time into JPEG files with exiftool (outside Lightroom), with a backup of every file.

Lightroom's SDK cannot set the capture time, so photos without one (files named like
``2019-12-08_19-35-23_000.jpeg``) get it written into the file; Lightroom reads it with
*Metadata > Read Metadata from Files*. Only JPEGs, only files without a capture time, and every file
is copied to the backup folder first and restored when exiftool fails.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .bridge import config_dir

EXIFTOOL_PATHS = ("/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool", "/usr/bin/exiftool")
JPEG_SUFFIXES = {".jpg", ".jpeg"}
TIMEOUT = 60

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def exiftool_path() -> str | None:
    """exiftool from PATH or the usual Homebrew locations (a LaunchAgent has a short PATH)."""
    return shutil.which("exiftool") or next((p for p in EXIFTOOL_PATHS if os.access(p, os.X_OK)), None)


def backup_root() -> Path:
    return config_dir() / "file-backups"


def exif_time(iso: str) -> str:
    """2019-09-10T08:24:16 -> 2019:09:10 08:24:16"""
    return iso[:10].replace("-", ":") + " " + iso[11:19]


def check_file(path: str | None, expected_name: str) -> str | None:
    """Why this file must not be touched, or None when it is fine."""
    if not path:
        return "Lightroom nennt keinen Pfad"
    p = Path(path)
    if not p.is_absolute() or p.name != expected_name:
        return "Pfad passt nicht zum Dateinamen"
    if p.suffix.lower() not in JPEG_SUFFIXES:
        return "kein JPEG"
    if p.is_symlink() or not p.is_file():
        return "Datei nicht gefunden"
    if p.stat().st_size == 0:
        return "Datei ist leer"
    if not os.access(p, os.W_OK):
        return "Datei ist nicht beschreibbar"
    return None


def existing_time(exiftool: str, path: str, runner: Runner = subprocess.run) -> str:
    """A capture time already inside the file ('' when there is none)."""
    done = runner([exiftool, "-s3", "-DateTimeOriginal", "-CreateDate", path],
                  capture_output=True, text=True, timeout=TIMEOUT, check=False)
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout or "exiftool failed").strip()[:200])
    return done.stdout.strip()


def _copy(source: Path | str, target: Path | str) -> None:
    """Copy the content (and, where allowed, the modification time) of a file.

    shutil.copy2 also copies flags and extended attributes, which macOS refuses for files from an SMB
    share ("Operation not permitted"); the content is what the backup is for.
    """
    shutil.copyfile(source, target)
    try:
        info = os.stat(source)
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    except OSError:
        pass


def write_time(exiftool: str, path: str, iso: str, runner: Runner = subprocess.run,
               root: Path | None = None) -> dict[str, str]:
    """Copy the file to the backup folder, write DateTimeOriginal and CreateDate, restore on failure."""
    stamp = time.strftime("%Y-%m-%d")
    folder = (root or backup_root()) / stamp
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = folder / (hashlib.sha1(path.encode()).hexdigest()[:8] + "_" + Path(path).name)
    _copy(path, backup)
    stamp_value = exif_time(iso)
    done = runner([exiftool, "-overwrite_original", "-P", "-m", f"-DateTimeOriginal={stamp_value}",
                   f"-CreateDate={stamp_value}", path],
                  capture_output=True, text=True, timeout=TIMEOUT, check=False)
    if done.returncode != 0 or "1 image files updated" not in done.stdout:
        _copy(backup, path)  # leave the file as it was
        raise RuntimeError((done.stderr or done.stdout or "exiftool failed").strip()[:200])
    return {"backup": str(backup)}

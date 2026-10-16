"""Hot folder: JPEGs dropped into a folder are sent to the MHPhotos Foto-Inbox.

Uploads land in the inbox with status "wartet" (review queue); nothing goes live from here.
Sent files move to ``gesendet/``, files the inbox rejects for good move to ``fehler/``.
The admin token comes from the same places as for the MCP tools (Keychain preferred) and is
never printed.
"""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import publishing

DEFAULT_DIR = Path.home() / "Pictures" / "MHPhotos-Inbox"
SENT_DIR = "gesendet"
FAILED_DIR = "fehler"
EXTENSIONS = {".jpg", ".jpeg"}  # upload() sends image/jpeg
SETTLE_SECONDS = 5  # a file must be unchanged this long (an export may still be writing it)
MAX_ATTEMPTS = 5
AUTH_RETRY_SECONDS = 300
SERVICE_LABEL = "de.mhphotos.inbox-watcher"
WATCH_DIR_ENV = "MHPHOTOS_WATCH_DIR"

Log = Callable[[str], None]


def _stamp_log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + message, flush=True)


def watch_dir() -> Path:
    raw = os.environ.get(WATCH_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_DIR


def _unique(target_dir: Path, name: str) -> Path:
    target = target_dir / name
    n = 1
    while target.exists():
        target = target_dir / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1
    return target


def _move(path: Path, sub: str) -> Path:
    dest_dir = path.parent / sub
    dest_dir.mkdir(exist_ok=True)
    dest = _unique(dest_dir, path.name)
    shutil.move(str(path), dest)
    return dest


def _http_status(error: str) -> int | None:
    match = re.search(r"HTTP (\d{3})", error)
    return int(match.group(1)) if match else None


class Watcher:
    def __init__(self, folder: Path, log: Log = _stamp_log, dry_run: bool = False) -> None:
        self.folder = folder
        self.log = log
        self.dry_run = dry_run
        self._attempts: dict[Path, tuple[int, float]] = {}  # file -> (failures, retry not before)
        self._auth_blocked_until = 0.0

    def candidates(self, now: float) -> list[Path]:
        out = []
        for path in sorted(self.folder.iterdir()):
            if path.name.startswith(".") or path.suffix.lower() not in EXTENSIONS:
                continue
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
            if info.st_size == 0 or now - info.st_mtime < SETTLE_SECONDS:
                continue
            out.append(path)
        return out

    def scan(self, cfg: publishing.InboxConfig | None, now: float | None = None) -> dict[str, int]:
        """One pass over the folder. Returns counts: sent, duplicate, failed, waiting."""
        now = time.time() if now is None else now
        counts = {"sent": 0, "duplicate": 0, "failed": 0, "waiting": 0}
        for path in self.candidates(now):
            if self.dry_run:
                self.log(f"[dry run] would send {path.name}")
                counts["waiting"] += 1
                continue
            if cfg is None or now < self._auth_blocked_until:
                counts["waiting"] += 1
                continue
            failures, not_before = self._attempts.get(path, (0, 0.0))
            if now < not_before:
                counts["waiting"] += 1
                continue
            result = publishing.upload(cfg, path, {})
            status = result.get("status")
            if status in {"uploaded", "duplicate"}:
                dest = _move(path, SENT_DIR)
                self._attempts.pop(path, None)
                key = "duplicate" if status == "duplicate" else "sent"
                counts[key] += 1
                label = "already in the inbox" if key == "duplicate" else f"inbox id {result.get('inbox_id')}"
                self.log(f"{path.name}: {label}, moved to {SENT_DIR}/{dest.name}")
                continue
            error = str(result.get("error", "unknown error"))
            code = _http_status(error)
            if code in {401, 403}:
                self._auth_blocked_until = now + AUTH_RETRY_SECONDS
                self.log(f"{path.name}: the inbox rejected the admin token (HTTP {code}); retrying in 5 minutes")
                counts["waiting"] += 1
                continue
            failures += 1
            permanent = (code is not None and 400 <= code < 500 and code not in {408, 429}) or "larger than" in error
            if permanent or failures >= MAX_ATTEMPTS:
                dest = _move(path, FAILED_DIR)
                dest.with_name(dest.name + ".fehler.txt").write_text(error + "\n")
                self._attempts.pop(path, None)
                counts["failed"] += 1
                self.log(f"{path.name}: {error}; moved to {FAILED_DIR}/ (see {dest.name}.fehler.txt)")
            else:
                delay = min(30 * 2 ** (failures - 1), 600)
                self._attempts[path] = (failures, now + delay)
                counts["waiting"] += 1
                self.log(f"{path.name}: {error}; attempt {failures}/{MAX_ATTEMPTS}, retrying in {delay}s")
        return counts


def run(folder: Path | None = None, once: bool = False, dry_run: bool = False, interval: float = 10.0) -> int:
    folder = folder or watch_dir()
    folder.mkdir(parents=True, exist_ok=True)
    watcher = Watcher(folder, dry_run=dry_run)
    watcher.log(f"watching {folder} (JPEG, sent files move to {SENT_DIR}/)")
    warned = False
    while True:
        publishing._keychain_cache.clear()  # a token stored after start-up is picked up
        cfg = publishing.inbox_config()
        if cfg is None and not dry_run:
            if not warned:
                watcher.log(publishing.config_missing_message())
                warned = True
            if once:
                return 2
        else:
            warned = False
        try:
            folder.mkdir(parents=True, exist_ok=True)  # recreated if it vanished (e.g. unmounted volume)
            counts = watcher.scan(cfg)
        except OSError as exc:
            # A broken folder must not end the process: launchd would restart it in a tight loop.
            watcher.log(f"cannot read {folder}: {type(exc).__name__}; trying again in {int(interval)}s")
            if once:
                return 1
            time.sleep(interval)
            continue
        if once:
            return 1 if counts["failed"] else 0
        time.sleep(interval)


def service_plist(folder: Path, url: str, python: str | None = None) -> bytes:
    """launchd agent for the watcher. Holds no secret: the token stays in the Keychain."""
    logs = Path.home() / "Library" / "Logs" / "MHPhotos"
    return plistlib.dumps(
        {
            "Label": SERVICE_LABEL,
            "ProgramArguments": [python or sys.executable, "-m", "mhphotos_lightroom_mcp.watcher"],
            "EnvironmentVariables": {publishing.URL_ENV: url, WATCH_DIR_ENV: str(folder)},
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "ProcessType": "Background",
            "StandardOutPath": str(logs / "inbox-watcher.log"),
            "StandardErrorPath": str(logs / "inbox-watcher.log"),
        }
    )


def install_service(url: str, folder: Path | None = None) -> tuple[int, str]:
    """Write the launchd plist (does not load it). Returns (exit code, message)."""
    if not re.match(r"^https://[^/\s]+/?$", url):
        return 2, "Give the https address of the MHPhotos app, e.g. https://example.de"
    folder = (folder or watch_dir()).expanduser()
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (Path.home() / "Library" / "Logs" / "MHPhotos").mkdir(parents=True, exist_ok=True)
    folder.mkdir(parents=True, exist_ok=True)
    target = agents / f"{SERVICE_LABEL}.plist"
    target.write_bytes(service_plist(folder, url))
    target.chmod(0o600)
    return 0, (
        f"Wrote {target}\nWatch folder: {folder}\n"
        f"Start it with:  launchctl bootstrap gui/$(id -u) {target}\n"
        f"Stop it with:   launchctl bootout gui/$(id -u)/{SERVICE_LABEL}\n"
        "Needs the admin token in the Keychain: security add-generic-password -s mhphotos-admin-token "
        '-a "$USER" -w   (you are prompted for the value)'
    )


def service_status() -> list[tuple[str, str, str]]:
    """Rows for ``doctor``: (status, label, detail). Read-only; never touches the token."""
    plist = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    if not plist.is_file():
        return [("ok", "inbox hot folder", "service not installed (optional: install-service)")]
    rows = []
    folder = watch_dir()
    try:
        folder = Path(plistlib.loads(plist.read_bytes())["EnvironmentVariables"][WATCH_DIR_ENV])
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
        rows.append(("warn", "inbox hot folder", f"{plist} is unreadable"))
    if sys.platform == "darwin":
        try:
            res = subprocess.run(
                ["/bin/launchctl", "print", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
                capture_output=True, timeout=5, check=False,
            )
            loaded = res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            loaded = False
        rows.append(("ok" if loaded else "warn", "inbox hot folder service",
                     "running" if loaded else f"installed but not loaded: launchctl bootstrap gui/$(id -u) {plist}"))
    if folder.is_dir():
        waiting = sum(1 for p in folder.iterdir() if p.suffix.lower() in EXTENSIONS and not p.name.startswith("."))
        failed = folder / FAILED_DIR
        n_failed = sum(1 for p in failed.iterdir() if p.suffix.lower() in EXTENSIONS) if failed.is_dir() else 0
        rows.append(("ok" if not n_failed else "warn", "inbox hot folder",
                     f"{folder}: {waiting} waiting, {n_failed} in {FAILED_DIR}/"))
    else:
        rows.append(("warn", "inbox hot folder", f"{folder} does not exist"))
    return rows


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()

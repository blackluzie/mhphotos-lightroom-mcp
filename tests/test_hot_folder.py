"""Hot folder: finished JPEGs from Lightroom's auto-export become verified export batches for n8n."""

import json
import os
import time

import pytest
from mhphotos_lightroom_mcp import hot_folder as hf
from mhphotos_lightroom_mcp.asset_delivery import completed_assets
from PIL import Image


def jpeg(path, colour="red"):
    Image.new("RGB", (60, 40), colour).save(path, "JPEG")
    old = time.time() - 60
    os.utime(path, (old, old))
    return path


def make(tmp_path):
    calls = []
    folder, exports = tmp_path / "hot", tmp_path / "exports"
    folder.mkdir()
    watcher = hf.HotFolder(folder, exports, lambda batch, files: calls.append((batch, files)) or True,
                           clock=lambda: time.time())
    return watcher, folder, exports, calls


def test_waits_until_a_file_is_stable_then_hands_it_to_n8n(tmp_path):
    watcher, folder, exports, calls = make(tmp_path)
    jpeg(folder / "IMG_1.jpg")
    assert watcher.scan_once()["files"] == 0  # first sighting: maybe still being written
    assert watcher.scan_once() == {"batches": 1, "files": 1, "rejected": 0}
    assert not (folder / "IMG_1.jpg").exists()
    [(batch, files)] = calls
    marker = json.loads((exports / batch / "ready.json").read_text())
    assert marker["files"][0]["name"] == "IMG_1.jpg" and marker["files"][0]["sha256"] == files[0]["sha256"]
    listed = completed_assets(exports)
    assert [m["filename"] for _, m in listed.values()] == ["IMG_1.jpg"]  # the asset service serves it


def test_growing_or_fresh_files_wait(tmp_path):
    watcher, folder, _, calls = make(tmp_path)
    path = jpeg(folder / "IMG_2.jpg")
    watcher.scan_once()
    with path.open("ab") as fh:
        fh.write(b"\0")  # Lightroom is still writing: the size changed since the first look
    old = time.time() - 60
    os.utime(path, (old, old))
    assert watcher.scan_once()["files"] == 0
    fresh = folder / "IMG_3.jpg"
    Image.new("RGB", (10, 10)).save(fresh, "JPEG")  # mtime = now: not settled yet
    assert watcher.scan_once()["files"] == 1 and calls[0][1][0]["path"].endswith("IMG_2.jpg")
    assert watcher.scan_once()["files"] == 0 and fresh.exists()  # still too fresh


def test_non_jpegs_go_to_fehler_and_batches_hold_twelve(tmp_path):
    watcher, folder, _, calls = make(tmp_path)
    (folder / "notes.txt").write_text("x")
    broken = folder / "IMG_bad.jpg"
    broken.write_bytes(b"not a jpeg")
    for i in range(13):
        jpeg(folder / f"IMG_{i:02d}.jpg")
    old = time.time() - 60
    for p in (folder / "notes.txt", broken):
        os.utime(p, (old, old))
    watcher.scan_once()
    res = watcher.scan_once()
    assert res == {"batches": 2, "files": 13, "rejected": 2}
    assert sorted(p.name for p in (folder / "fehler").iterdir()) == ["IMG_bad.jpg", "notes.txt"]
    assert [len(files) for _, files in calls] == [12, 1]


def test_missing_folder_is_quiet(tmp_path):
    watcher = hf.HotFolder(tmp_path / "nope", tmp_path / "e", lambda *_: None)
    assert watcher.scan_once() == {"batches": 0, "files": 0, "rejected": 0}


def test_rebuild_hot_folder_needs_n8n(tmp_path):
    from mhphotos_lightroom_mcp.rebuild import configure

    from .test_rebuild import GITHUB, setup
    home, secret = setup(tmp_path)
    configure(home, tmp_path, tmp_path / "python", "https://photos.example",
              ["https://client.example/callback"], secret, True, GITHUB)
    with pytest.raises(ValueError, match="n8n"):
        configure(home, tmp_path, tmp_path / "python", "https://photos.example", [], secret, True,
                  hot_folder=str(home / "Pictures/MHPhotos-Inbox"))
    plan = configure(home, tmp_path, tmp_path / "python", "https://photos.example", [], secret, True,
                     n8n_webhook_url="https://n8n.example/webhook/x", hot_folder=str(home / "Pictures/MHPhotos-Inbox"))
    assert plan["hot_folder"].endswith("Pictures/MHPhotos-Inbox") and "warning" not in plan
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "de.mhphotos.inbox-watcher.plist").write_text("x")
    plan = configure(home, tmp_path, tmp_path / "python", "https://photos.example", [], secret, False)
    assert "inbox-watcher" in plan["warning"]
    plan = configure(home, tmp_path, tmp_path / "python", "https://photos.example", [], secret, True, hot_folder=False)
    assert plan["hot_folder"] is None

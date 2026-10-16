"""Hot-folder service against a local fake inbox (same endpoint the MHPhotos app exposes)."""

import json
import plistlib
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from mhphotos_lightroom_mcp import cli, publishing, watcher

NOW = 1_000_000.0


class _Inbox:
    def __init__(self, status=200, duplicate=False):
        self.status = status
        self.duplicate = duplicate
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append({"path": self.path, "token": self.headers.get("X-Admin-Token"), "body": body})
                raw = json.dumps({"ok": True, "duplicate": outer.duplicate, "item": {"id": f"lr-{len(outer.requests)}"}}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    @property
    def cfg(self):
        return publishing.InboxConfig(f"http://127.0.0.1:{self._srv.server_port}", "secret")

    def stop(self):
        self._srv.shutdown()


@pytest.fixture
def inbox():
    box = _Inbox()
    yield box
    box.stop()


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "hot"
    d.mkdir()
    return d


def _photo(folder, name="a.jpg", age=60, data=b"\xff\xd8jpegdata"):
    path = folder / name
    path.write_bytes(data)
    import os

    os.utime(path, (NOW - age, NOW - age))
    return path


def test_sends_new_jpeg_and_moves_it(inbox, folder):
    _photo(folder)
    logs = []
    counts = watcher.Watcher(folder, logs.append).scan(inbox.cfg, NOW)
    assert counts["sent"] == 1
    assert (folder / "gesendet" / "a.jpg").is_file() and not (folder / "a.jpg").exists()
    assert inbox.requests[0]["path"] == "/api/admin/lightroom-import"
    assert inbox.requests[0]["token"] == "secret"
    assert not any("secret" in line for line in logs)


def test_duplicate_is_moved_too(folder):
    box = _Inbox(duplicate=True)
    try:
        _photo(folder)
        assert watcher.Watcher(folder, lambda m: None).scan(box.cfg, NOW)["duplicate"] == 1
        assert (folder / "gesendet" / "a.jpg").is_file()
    finally:
        box.stop()


def test_ignores_young_hidden_non_jpeg_and_empty_files(inbox, folder):
    _photo(folder, "young.jpg", age=1)
    _photo(folder, ".hidden.jpg")
    _photo(folder, "note.txt")
    _photo(folder, "empty.jpg", data=b"")
    (folder / "sub").mkdir()
    assert watcher.Watcher(folder, lambda m: None).scan(inbox.cfg, NOW) == {
        "sent": 0, "duplicate": 0, "failed": 0, "waiting": 0}
    assert inbox.requests == []


def test_name_clash_in_sent_folder_keeps_both(inbox, folder):
    _photo(folder)
    w = watcher.Watcher(folder, lambda m: None)
    w.scan(inbox.cfg, NOW)
    _photo(folder)
    w.scan(inbox.cfg, NOW)
    assert sorted(p.name for p in (folder / "gesendet").iterdir()) == ["a-1.jpg", "a.jpg"]


def test_rejected_token_keeps_the_file_and_waits(folder):
    box = _Inbox(status=401)
    try:
        path = _photo(folder)
        logs = []
        w = watcher.Watcher(folder, logs.append)
        assert w.scan(box.cfg, NOW)["waiting"] == 1
        assert path.exists() and "rejected the admin token" in logs[0]
        assert w.scan(box.cfg, NOW + 10)["waiting"] == 1
        assert len(box.requests) == 1  # no hammering while blocked
    finally:
        box.stop()


def test_server_error_retries_then_gives_up(folder):
    box = _Inbox(status=500)
    try:
        path = _photo(folder)
        w = watcher.Watcher(folder, lambda m: None)
        t = NOW
        for _ in range(watcher.MAX_ATTEMPTS - 1):
            assert w.scan(box.cfg, t)["waiting"] == 1
            t += 1000
        assert path.exists()
        assert w.scan(box.cfg, t)["failed"] == 1
        assert (folder / "fehler" / "a.jpg").is_file()
        assert "HTTP 500" in (folder / "fehler" / "a.jpg.fehler.txt").read_text()
    finally:
        box.stop()


def test_permanent_client_error_goes_to_fehler_at_once(folder):
    box = _Inbox(status=400)
    try:
        _photo(folder)
        assert watcher.Watcher(folder, lambda m: None).scan(box.cfg, NOW)["failed"] == 1
    finally:
        box.stop()


def test_no_config_waits_without_touching_files(folder):
    path = _photo(folder)
    assert watcher.Watcher(folder, lambda m: None).scan(None, NOW)["waiting"] == 1
    assert path.exists()


def test_dry_run_sends_nothing(inbox, folder):
    path = _photo(folder)
    logs = []
    watcher.Watcher(folder, logs.append, dry_run=True).scan(inbox.cfg, NOW)
    assert path.exists() and inbox.requests == [] and "would send a.jpg" in logs[0]


def test_run_once_without_config_is_an_error(folder, capsys):
    assert watcher.run(folder, once=True) == 2
    assert "not configured" in capsys.readouterr().out


def test_run_once_sends(inbox, folder, monkeypatch):
    monkeypatch.setenv("MHPHOTOS_INBOX_URL", inbox.cfg.base_url)
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "secret")
    _photo(folder, age=time.time() - NOW + 60)  # old enough in real time
    assert watcher.run(folder, once=True) == 0
    assert (folder / "gesendet" / "a.jpg").is_file()


def test_service_plist_holds_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "supersecret")
    code, message = watcher.install_service("https://example.de", tmp_path / "hot")
    assert code == 0 and "launchctl bootstrap" in message
    raw = (tmp_path / "Library/LaunchAgents/de.mhphotos.inbox-watcher.plist").read_bytes()
    plist = plistlib.loads(raw)
    assert b"supersecret" not in raw and "TOKEN" not in " ".join(plist["EnvironmentVariables"])
    assert plist["EnvironmentVariables"]["MHPHOTOS_INBOX_URL"] == "https://example.de"
    assert plist["KeepAlive"] is True


def test_install_service_needs_https_address(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert watcher.install_service("http://example.de")[0] == 2
    assert watcher.install_service("example.de")[0] == 2


def test_cli_usage_mentions_the_commands(capsys):
    assert cli.run(["install-service"]) == 2
    assert "install-service" in capsys.readouterr().out


def test_vanished_folder_does_not_end_the_service(folder, monkeypatch, capsys):
    import shutil

    shutil.rmtree(folder)
    calls = {"n": 0}

    def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] > 1:
            raise KeyboardInterrupt

    original = watcher.Watcher.scan

    def flaky(self, cfg, now=None):
        if calls["n"] == 0:
            raise FileNotFoundError("gone")
        return original(self, cfg, now)

    monkeypatch.setattr(watcher.time, "sleep", fake_sleep)
    monkeypatch.setattr(watcher.Watcher, "scan", flaky)
    with pytest.raises(KeyboardInterrupt):
        watcher.run(folder)
    assert "cannot read" in capsys.readouterr().out and folder.is_dir()


def test_doctor_rows_without_and_with_the_service(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert watcher.service_status()[0][2].startswith("service not installed")
    hot = tmp_path / "hot"
    watcher.install_service("https://example.de", hot)
    (hot / "fehler").mkdir()
    (hot / "fehler" / "x.jpg").write_bytes(b"x")
    (hot / "new.jpg").write_bytes(b"x")
    rows = {label: (status, detail) for status, label, detail in watcher.service_status()}
    assert rows["inbox hot folder"][0] == "warn" and "1 waiting, 1 in fehler/" in rows["inbox hot folder"][1]

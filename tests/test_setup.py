"""Token sources, toolsets, self test, doctor, install-plugin."""

import json
import os
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mhphotos_lightroom_mcp import bridge, cli, publishing, server
from mhphotos_lightroom_mcp.bridge import Bridge, BridgeAdapter, write_plugin_config

from .fake_mhphotos import FakeMHPhotos
from .fake_plugin import FakePlugin

pytestmark = pytest.mark.anyio


# ---- token sources ---------------------------------------------------------
def test_token_priority_env_file_keychain(monkeypatch, tmp_path):
    assert publishing.resolve_token()[0] is None
    # keychain (only consulted on macOS)
    monkeypatch.setattr(publishing.sys, "platform", "darwin")
    publishing._keychain_cache.clear()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="from-keychain\n", stderr="")

    monkeypatch.setattr(publishing.subprocess, "run", fake_run)
    assert publishing.resolve_token() == ("from-keychain", "macOS Keychain (mhphotos-admin-token)")
    publishing.resolve_token()
    assert len(calls) == 1  # cached
    # file beats keychain
    f = tmp_path / "tok"
    f.write_text("from-file\n")
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN_FILE", str(f))
    assert publishing.resolve_token()[0] == "from-file"
    # env beats file
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "from-env")
    assert publishing.resolve_token()[0] == "from-env"


def test_token_file_must_be_absolute_and_readable(monkeypatch, tmp_path):
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN_FILE", "relative/token")
    assert publishing.resolve_token()[0] is None
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN_FILE", str(tmp_path / "missing"))
    assert publishing.resolve_token()[0] is None
    (tmp_path / "empty").write_text("  \n")
    monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN_FILE", str(tmp_path / "empty"))
    assert publishing.resolve_token()[0] is None


def test_keychain_not_used_off_macos(monkeypatch):
    monkeypatch.setattr(publishing.sys, "platform", "linux")
    publishing._keychain_cache.clear()

    def boom(*a, **k):
        raise AssertionError("must not run `security` off macOS")

    monkeypatch.setattr(publishing.subprocess, "run", boom)
    assert publishing.resolve_token()[0] is None


async def test_inbox_works_with_token_from_file(monkeypatch, tmp_path):
    app = FakeMHPhotos()
    try:
        (tmp_path / "t").write_text("secret\n")
        monkeypatch.setenv("MHPHOTOS_INBOX_URL", app.url)
        monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN_FILE", str(tmp_path / "t"))
        assert len((await server.mhphotos_galleries())["galleries"]) == 2
    finally:
        app.stop()


# ---- toolsets --------------------------------------------------------------
def _tool_names(**env):
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mhphotos_lightroom_mcp.server"],
            env={**os.environ, **env},
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            return {t.name for t in (await s.list_tools()).tools}

    return anyio.run(run)


def test_toolsets_limit_the_tools():
    names = _tool_names(MHPHOTOS_TOOLSETS="read")
    assert "list_photos" in names and "get_photo_previews" in names and "lightroom_selftest" in names
    assert not names & {"set_rating", "apply_preset", "mhphotos_publish", "export_photos"}
    both = _tool_names(MHPHOTOS_TOOLSETS="read,develop")
    assert "apply_preset" in both and "set_rating" not in both
    assert "set_rating" in _tool_names()  # default: everything


def test_unknown_toolset_fails_fast():
    res = subprocess.run(
        [sys.executable, "-m", "mhphotos_lightroom_mcp.server"],
        env={**os.environ, "MHPHOTOS_TOOLSETS": "read,bogus"},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert res.returncode != 0 and "Unknown toolset" in res.stderr


def test_every_toolset_name_is_a_real_tool():
    registered = {t.name for t in anyio.run(server.mcp.list_tools)}
    listed = set().union(*server.TOOLSETS.values())
    assert listed - registered == set(), "toolset lists unknown tools"
    assert registered - listed == set(), "tool missing from every toolset"


# ---- self test -------------------------------------------------------------
async def test_selftest_mock():
    res = await server.lightroom_selftest()
    assert res["all_ok"] is True


async def test_selftest_through_bridge():
    b = Bridge(poll_wait=0.5, connected_ttl=2.0)
    b.start()
    plugin = FakePlugin(b).start()
    try:
        server.set_adapter(BridgeAdapter(b, timeout=5))
        with anyio.fail_after(5):
            while not b.is_connected():
                await anyio.sleep(0.05)
        res = await server.lightroom_selftest()
        assert res["all_ok"] is True and res["checks"][0]["name"] == "fake plugin"
    finally:
        plugin.stop()
        b.stop()


# ---- doctor / install-plugin ----------------------------------------------
def test_doctor_reports_without_server(monkeypatch):
    code, rows = cli.doctor()
    text = " ".join(f"{s} {a} {b}" for s, a, b in rows)
    assert code == 0
    assert "edits (MHPHOTOS_ALLOW_WRITES): off" in text.replace("  ", " ") or "off (read-only)" in text
    assert "not set (MHPhotos tools disabled)" in text


def test_doctor_with_running_bridge_and_app(monkeypatch, tmp_path):
    b = Bridge(poll_wait=0.5, connected_ttl=2.0)
    b.start()
    app = FakeMHPhotos()
    plugin = FakePlugin(b).start()
    try:
        write_plugin_config(b)
        monkeypatch.setenv("MHPHOTOS_INBOX_URL", app.url)
        monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "secret")
        monkeypatch.setenv("MHPHOTOS_EXPORT_DIR", str(tmp_path / "exp"))
        import time

        deadline = time.monotonic() + 5
        while not b.is_connected() and time.monotonic() < deadline:
            time.sleep(0.05)
        code, rows = cli.doctor()
        d = {label: (status, detail) for status, label, detail in rows}
        assert code == 0
        assert d["MCP server"][0] == cli.OK
        assert d["Lightroom plugin"] == (cli.OK, "connected")
        assert d["MHPhotos admin token"][0] == cli.OK
        assert d["MHPhotos app"][0] == cli.OK and "token accepted" in d["MHPhotos app"][1]
        assert "secret" not in json.dumps(rows)  # the token value never appears
    finally:
        plugin.stop()
        b.stop()
        app.stop()


def test_doctor_flags_bad_token(monkeypatch):
    app = FakeMHPhotos()
    try:
        monkeypatch.setenv("MHPHOTOS_INBOX_URL", app.url)
        monkeypatch.setenv("MHPHOTOS_ADMIN_TOKEN", "wrong")
        code, rows = cli.doctor()
        assert code == 1
        assert any(s == cli.FAIL and "rejected" in d for s, _, d in rows)
    finally:
        app.stop()


def test_doctor_flags_missing_token(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_INBOX_URL", "https://mhphotos.example")
    code, rows = cli.doctor()
    assert code == 1
    assert any(s == cli.FAIL and "security add-generic-password" in d for s, _, d in rows)


def test_install_plugin(tmp_path):
    code, _ = cli.install_plugin(str(tmp_path / "dest"))
    target = tmp_path / "dest" / "mhphotos.lrdevplugin"
    assert code == 0 and (target / "Info.lua").is_file() and (target / "Commands.lua").is_file()
    assert not list(target.rglob("*.pyc"))
    code, _ = cli.install_plugin(str(tmp_path / "dest"))  # reinstall replaces cleanly
    assert code == 0 and (target / "Info.lua").is_file()


def test_install_plugin_refuses_foreign_folder(tmp_path):
    foreign = tmp_path / "dest" / "mhphotos.lrdevplugin"
    foreign.mkdir(parents=True)
    (foreign / "precious.txt").write_text("keep me")
    code, _ = cli.install_plugin(str(tmp_path / "dest"))
    assert code == 1 and (foreign / "precious.txt").read_text() == "keep me"


def test_cli_usage():
    assert cli.run(["--help"]) == 0
    assert cli.run(["nonsense"]) == 2
    assert Path(cli.plugin_source() or "").name == "mhphotos.lrdevplugin"


def test_bridge_health_reports_plugin_state():
    b = Bridge()
    b.start()
    try:
        assert cli._health(b.url, b.token) == {"ok": True, "plugin_connected": False}
        assert cli._health(b.url, "wrong") is None
    finally:
        b.stop()


def test_config_dir_override():
    assert bridge.config_dir() == Path(os.environ["MHPHOTOS_CONFIG_DIR"])

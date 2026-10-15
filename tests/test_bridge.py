import json
import time
import urllib.error
import urllib.request

import pytest
from mhphotos_lightroom_mcp import server
from mhphotos_lightroom_mcp.bridge import (
    Bridge,
    BridgeAdapter,
    BridgeUnavailable,
    write_plugin_config,
)

from .fake_plugin import FakePlugin

pytestmark = pytest.mark.anyio


@pytest.fixture
def bridge():
    b = Bridge(poll_wait=0.5, connected_ttl=2.0)
    b.start()
    yield b
    b.stop()


@pytest.fixture
def plugin(bridge):
    p = FakePlugin(bridge).start()
    server.set_adapter(BridgeAdapter(bridge, timeout=5))
    deadline = time.monotonic() + 5
    while not bridge.is_connected() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert bridge.is_connected()
    yield p
    p.stop()


def _req(bridge, path, token=None, data=None, method="POST"):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        bridge.url + path,
        data=None if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode()),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_rejects_non_loopback_bind():
    with pytest.raises(ValueError):
        Bridge(host="0.0.0.0")


def test_auth_required(bridge):
    assert _req(bridge, "/v1/poll", token=None, data={}) == 401
    assert _req(bridge, "/v1/poll", token="wrong", data={}) == 401
    assert _req(bridge, "/v1/health", token="wrong", method="GET") == 401
    assert _req(bridge, "/v1/health", token=bridge.token, method="GET") == 200


def test_bad_requests(bridge):
    assert _req(bridge, "/v1/poll", token=bridge.token, data=b"not json") == 400
    assert _req(bridge, "/v1/poll", token=bridge.token, data=b"[1]") == 400
    assert _req(bridge, "/v1/nope", token=bridge.token, data={}) == 404
    assert _req(bridge, "/v1/result", token=bridge.token, data={"id": "unknown"}) == 404
    big = b'{"x":"' + b"a" * 1_100_000 + b'"}'
    try:  # server may also reset the connection while the oversize body is still being sent
        assert _req(bridge, "/v1/poll", token=bridge.token, data=big) == 400
    except (urllib.error.URLError, ConnectionError):
        pass


async def test_unavailable_when_plugin_missing(bridge):
    server.set_adapter(BridgeAdapter(bridge, timeout=1))
    status = await server.connection_status()
    assert status["connected"] is False
    assert "Waiting for the Lightroom" in status["message"]
    res = await server.list_collections()
    assert res["code"] == "unavailable"


async def test_roundtrip_reads(bridge, plugin):
    status = await server.connection_status()
    assert status["connected"] is True
    assert status["mode"] == "bridge"
    colls = (await server.list_collections())["collections"]
    assert {c["name"] for c in colls} == {"Portraits", "Landschaft", "Leer"}
    page = await server.list_photos(collection_id="c1", limit=2)
    assert page["total"] == 3 and len(page["photos"]) == 2
    meta = await server.get_photo_metadata("p4")
    assert meta["camera"] == "Nikon Z8"
    assert "error" in await server.get_photo_metadata("nope")  # not_found mapped


async def test_roundtrip_gated_writes_and_export(bridge, plugin, monkeypatch, tmp_path):
    assert (await server.set_rating(["p2"], 1, confirm=True))["code"] == "disabled"
    assert "set_rating" not in plugin.commands
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    monkeypatch.setenv("MHPHOTOS_EXPORT_DIR", str(tmp_path / "exp"))
    dry = await server.set_rating(["p2"], 1)
    assert dry["dry_run"] and "set_rating" not in plugin.commands
    assert (await server.set_rating(["p2"], 1, confirm=True))["applied"]
    assert (await server.get_photo_metadata("p2"))["rating"] == 1
    assert (await server.set_flag(["p2"], "rejected", confirm=True))["applied"]
    assert (await server.add_keywords(["p2"], ["k"], confirm=True))["applied"]
    out = await server.export_photos(["p1"], confirm=True)
    assert out["exported"] == ["IMG_0001.jpg"]
    assert plugin.commands.count("export_photos") == 1


async def test_timeout_when_plugin_hangs(bridge):
    # connected (recent poll) but never answers
    _req(bridge, "/v1/poll", token=bridge.token, data={})
    adapter = BridgeAdapter(bridge, timeout=0.5)
    with pytest.raises(BridgeUnavailable):
        adapter.list_collections()


def test_plugin_config_file_is_private(bridge, tmp_path):
    path = write_plugin_config(bridge)
    cfg = json.loads(path.read_text())
    assert cfg == {"url": bridge.url, "token": bridge.token}
    assert (path.stat().st_mode & 0o077) == 0

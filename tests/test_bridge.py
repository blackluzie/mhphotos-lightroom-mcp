import json
import time
import urllib.error
import urllib.request

import pytest
from mhphotos_lightroom_mcp import server
from mhphotos_lightroom_mcp.bridge import (
    MAX_BODY,
    Bridge,
    BridgeAdapter,
    BridgeError,
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


def test_empty_list_body_counts_as_empty_object(bridge):
    # The Lua JSON encoder writes an empty table as [] and the real plugin polls with one.
    assert _req(bridge, "/v1/poll", token=bridge.token, data=b"[]") == 200
    assert bridge.is_connected()


def test_bad_requests(bridge):
    assert _req(bridge, "/v1/poll", token=bridge.token, data=b"not json") == 400
    assert _req(bridge, "/v1/poll", token=bridge.token, data=b"[1]") == 400
    assert _req(bridge, "/v1/nope", token=bridge.token, data={}) == 404
    assert _req(bridge, "/v1/result", token=bridge.token, data={"id": "unknown"}) == 404
    big = b'{"x":"' + b"a" * (MAX_BODY + 100_000) + b'"}'
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
    found = await server.list_photos(min_rating=5, year=2026)  # filters travel through the bridge
    assert [p["id"] for p in found["photos"]] == ["p1"]
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


def test_command_carries_remaining_timeout(bridge):
    # The plugin answers (with an error if need be) before the server gives up, so it keeps polling
    # instead of going silent; it learns the deadline from the command.
    import threading

    _req(bridge, "/v1/poll", token=bridge.token, data={})
    caller = threading.Thread(target=BridgeAdapter(bridge).list_collections, args=(True,), daemon=True)
    caller.start()
    req = urllib.request.Request(
        bridge.url + "/v1/poll",
        data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {bridge.token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        cmd = json.loads(r.read())["command"]
    assert cmd["name"] == "list_collections"
    assert 100 <= cmd["timeout"] <= 120
    _req(bridge, "/v1/result", token=bridge.token, data={"id": cmd["id"], "ok": True, "data": {"collections": []}})
    caller.join(5)


class _JobPlugin:
    """Stands in for Bridge.call: answers create_virtual_copies with a job and virtual_copy_job with scripted states."""

    def __init__(self, states, start=None):
        self.states = list(states)
        self.start = start or {"job_id": "vc1-100", "state": "running", "phase": "queued", "total": 2, "copies": []}
        self.calls = []

    def call(self, name, args=None, timeout=30.0):
        self.calls.append((name, args, timeout))
        if name == "create_virtual_copies":
            return self.start
        assert name == "virtual_copy_job" and args == {"job_id": "vc1-100"}
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


def _job(state, copies=(), **extra):
    return {"job_id": "vc1-100", "state": state, "total": 2,
            "copies": [{"source_id": s, "id": f"{s}-copy"} for s in copies], **extra}


def _adapter(plugin, wait=2.0):
    adapter = BridgeAdapter(plugin)
    adapter.copy_poll_every = 0.01
    adapter.copy_wait = wait
    return adapter


def test_virtual_copies_start_a_job_and_poll_its_status():
    # 0.1.25: the plug-in answers at once with a job; no bridge call waits for Lightroom longer than 30 s
    plugin = _JobPlugin([_job("running", phase="copying"), _job("running", ["u1"], phase="reading ids"),
                         _job("done", ["u1", "u2"])])
    out = _adapter(plugin).create_virtual_copies(["u1", "u2"], "Insta 9:16")
    assert out["state"] == "done" and out["job_id"] == "vc1-100"
    assert out["copies"] == [{"source_id": "u1", "id": "u1-copy"}, {"source_id": "u2", "id": "u2-copy"}]
    assert plugin.calls[0] == ("create_virtual_copies", {"photo_ids": ["u1", "u2"], "copy_name": "Insta 9:16"}, 30.0)
    assert all(timeout <= 30.0 for _, _, timeout in plugin.calls)
    assert [c[0] for c in plugin.calls].count("virtual_copy_job") == 3


def test_virtual_copies_answer_running_after_the_wait():
    plugin = _JobPlugin([_job("running", ["u1"], phase="reading ids", elapsed=40)])
    t0 = time.monotonic()
    out = _adapter(plugin, wait=0.2).create_virtual_copies(["u1", "u2"])
    assert time.monotonic() - t0 < 2
    assert out["state"] == "running" and out["job_id"] == "vc1-100" and out["copies"][0]["id"] == "u1-copy"
    assert "virtual_copy_job" in out["hint"]


def test_virtual_copy_job_failures():
    plugin = _JobPlugin([_job("timeout", ["u1"], error="Lightroom did not create all virtual copies within 23 s")])
    with pytest.raises(BridgeError, match="virtual_copy_timeout: .*within 23 s.*Copies made: u1-copy"):
        _adapter(plugin).create_virtual_copies(["u1", "u2"])
    # the status tool reports the same as a result, not an exception
    status = _adapter(plugin).virtual_copy_job("vc1-100")
    assert status["error"].startswith("virtual_copy_timeout") and status["copies"][0]["id"] == "u1-copy"


def test_virtual_copies_from_a_plugin_before_0_1_25():
    plugin = _JobPlugin([], start={"copies": [{"source_id": "u1", "id": "c1"}]})
    assert _adapter(plugin).create_virtual_copies(["u1"]) == {"copies": [{"source_id": "u1", "id": "c1"}]}
    assert len(plugin.calls) == 1


def test_plugin_config_file_is_private(bridge, tmp_path):
    path = write_plugin_config(bridge)
    cfg = json.loads(path.read_text())
    assert cfg == {"url": bridge.url, "token": bridge.token}
    assert (path.stat().st_mode & 0o077) == 0


async def test_roundtrip_editing_tools(bridge, plugin, monkeypatch):
    out = await server.get_photo_previews(["p4"], size=512)
    assert out[1].data[:2] == b"\xff\xd8"  # base64 survived the bridge
    assert (await server.get_develop_settings("p4"))["settings"]["Exposure2012"] == 0.0
    near = await server.list_photos(near_lat=46.7, near_lon=11.4, radius_km=40)
    assert near["total"] == 2
    assert len((await server.list_presets())["presets"]) == 3
    assert (await server.apply_preset(["p4"], "Vivid", confirm=True))["code"] == "disabled"
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    assert (await server.apply_preset(["p4"], "Vivid", confirm=True))["applied"]
    assert (await server.apply_develop_settings(["p4"], {"Exposure2012": 0.4}, confirm=True))["applied"]
    assert (await server.create_preset("Neu", {"Contrast2012": 5}, confirm=True))["applied"]
    assert plugin.commands.count("apply_develop_settings") == 1


# ---------------------------------------------------------------- 0.1.28: reconnect, big batches
class _WritePlugin:
    """Stands in for Bridge.call and records each write part; fails the part number `fail_at` (1-based)."""

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    def call(self, name, args=None, timeout=30.0):
        self.calls.append((name, args, timeout))
        if self.fail_at == len(self.calls):
            raise BridgeUnavailable(f"Lightroom did not answer '{name}' within {timeout:.0f}s.")
        return {"updated": len(args["photo_ids"])}


def test_big_writes_go_to_lightroom_in_parts():
    # 100 photos in one call ran past the time limit (45 worked); parts of 25 each get their own limit
    plugin = _WritePlugin()
    ids = [f"u{i}" for i in range(100)]
    out = BridgeAdapter(plugin).set_title_caption(ids, "Verona", None)
    assert out == {"updated": 100}
    assert [len(a["photo_ids"]) for _, a, _ in plugin.calls] == [25, 25, 25, 25]
    assert all(a["title"] == "Verona" and "caption" not in a for _, a, _ in plugin.calls)
    assert [p for _, a, _ in plugin.calls for p in a["photo_ids"]] == ids


def test_failed_part_says_which_photos_were_already_changed():
    plugin = _WritePlugin(fail_at=3)
    ids = [f"u{i}" for i in range(60)]
    with pytest.raises(BridgeUnavailable) as err:
        BridgeAdapter(plugin).set_rating(ids, 4)
    assert "first 50 of 60 photos were already changed" in str(err.value)
    assert len(plugin.calls) == 3


async def test_batch_reads_one_round_for_many_photos(bridge, plugin, monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    dry = await server.set_rating(["p1", "p2", "p3"], 2)
    assert dry["dry_run"] and [p["id"] for p in dry["photos"]] == ["p1", "p2", "p3"]
    assert plugin.commands.count("get_photos") == 1 and "get_photo" not in plugin.commands
    bad = await server.set_rating(["p1", "nope"], 2)
    assert "nope" in bad["error"]
    sheet = await server.get_contact_sheet(["p1", "p2", "nope"])
    legend = sheet[0]["legend"]
    assert [entry.get("error") for entry in legend] == [None, None, "not available"]
    assert plugin.commands.count("get_previews") == 1 and "get_preview" not in plugin.commands


async def test_batch_reads_fall_back_for_an_older_plugin(bridge):
    old = FakePlugin(bridge, legacy=True).start()
    try:
        server.set_adapter(BridgeAdapter(bridge, timeout=5))
        assert bridge.wait_connected(5)
        sheet = await server.get_contact_sheet(["p1", "p2"])
        assert [entry.get("error") for entry in sheet[0]["legend"]] == [None, None]
        assert old.commands.count("get_preview") == 2 and old.commands.count("get_photo") == 2
    finally:
        old.stop()


def test_call_waits_for_the_plugin_to_come_back():
    # the plug-in's watchdog replaces a hanging polling task within about a minute; the call waits for it
    import threading

    b = Bridge(poll_wait=0.5, connected_ttl=2.0, reconnect_wait=5.0)
    b.start()
    try:
        late = FakePlugin(b)
        threading.Timer(1.0, late.start).start()
        started = time.monotonic()
        data = BridgeAdapter(b, timeout=5).list_collections()
        assert data and 0.8 < time.monotonic() - started < 5
        late.stop()
    finally:
        b.stop()
    quick = Bridge(poll_wait=0.5, connected_ttl=2.0, reconnect_wait=0.5)
    quick.start()
    try:
        with pytest.raises(BridgeUnavailable, match="Waited 0s|Waited 1s"):
            quick.call("list_collections")
    finally:
        quick.stop()

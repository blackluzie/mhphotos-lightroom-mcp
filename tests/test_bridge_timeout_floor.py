import time

from mhphotos_lightroom_mcp.bridge import TIMEOUT_ENV, Bridge, timeout_floor

from .fake_plugin import FakePlugin


def test_floor_parsing(monkeypatch):
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)
    assert timeout_floor() == 0
    monkeypatch.setenv(TIMEOUT_ENV, "120")
    assert timeout_floor() == 120
    monkeypatch.setenv(TIMEOUT_ENV, "99999")
    assert timeout_floor() == 900
    monkeypatch.setenv(TIMEOUT_ENV, "abc")
    assert timeout_floor() == 0
    monkeypatch.setenv(TIMEOUT_ENV, "-5")
    assert timeout_floor() == 0


def test_floor_raises_short_timeout(monkeypatch):
    b = Bridge(poll_wait=0.5, connected_ttl=2.0)
    b.start()
    try:
        plugin = FakePlugin(b).start()
        deadline = time.monotonic() + 5
        while not b.is_connected() and time.monotonic() < deadline:
            time.sleep(0.05)
        monkeypatch.setenv(TIMEOUT_ENV, "30")
        t0 = time.monotonic()
        # a command the fake plugin answers normally still works with the floor on
        b.call("selftest", timeout=1.0)
        assert time.monotonic() - t0 < 5
        plugin.stop()
    finally:
        b.stop()

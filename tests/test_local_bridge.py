import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from mhphotos_lightroom_mcp.local_bridge import make_server

TOKEN = "test-only-token-not-for-production-123456789"


@pytest.fixture
def bridge():
    server = make_server(TOKEN, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(bridge, token=TOKEN, path="/v1/status", method="GET"):
    return Request(
        f"http://127.0.0.1:{bridge.server_port}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method=method,
    )


def test_authenticated_status_does_not_claim_lightroom_connection(bridge):
    with urlopen(request(bridge), timeout=2) as response:
        data = json.load(response)
    assert data["bridge_running"] is True
    assert data["connected"] is False
    assert data["lightroom_connected"] is False


def test_invalid_token_is_rejected(bridge):
    with pytest.raises(HTTPError) as exc:
        urlopen(request(bridge, token="wrong"), timeout=2)
    assert exc.value.code == 401


def test_unknown_route_is_rejected(bridge):
    with pytest.raises(HTTPError) as exc:
        urlopen(request(bridge, path="/v1/photos"), timeout=2)
    assert exc.value.code == 404


def test_writes_are_rejected(bridge):
    with pytest.raises(HTTPError) as exc:
        urlopen(request(bridge, method="POST"), timeout=2)
    assert exc.value.code == 405


def test_short_token_rejected():
    with pytest.raises(ValueError):
        make_server("too-short")

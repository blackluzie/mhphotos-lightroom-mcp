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


def request(bridge, token=TOKEN, path="/v1/status", method="GET", payload=None):
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    return Request(
        f"http://127.0.0.1:{bridge.server_port}{path}",
        headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None,
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


def test_other_post_routes_are_rejected(bridge):
    with pytest.raises(HTTPError) as exc:
        urlopen(request(bridge, method="POST"), timeout=2)
    assert exc.value.code == 404


def test_selection_roundtrip(bridge):
    payload = {"photos": [{"fileName": "IMG_0001.CR3", "rating": 5}]}
    with urlopen(
        request(bridge, path="/v1/selection", method="POST", payload=payload),
        timeout=2,
    ) as response:
        assert json.load(response)["accepted"] == 1
    with urlopen(request(bridge, path="/v1/selection"), timeout=2) as response:
        assert json.load(response) == payload


def test_selection_rejects_extra_fields(bridge):
    payload = {"photos": [{"fileName": "test", "absolutePath": "/private"}]}
    with pytest.raises(HTTPError) as exc:
        urlopen(
            request(bridge, path="/v1/selection", method="POST", payload=payload),
            timeout=2,
        )
    assert exc.value.code == 400


def test_selection_requires_authentication(bridge):
    with pytest.raises(HTTPError) as exc:
        urlopen(
            request(
                bridge,
                token="wrong",
                path="/v1/selection",
                method="POST",
                payload={"photos": []},
            ),
            timeout=2,
        )
    assert exc.value.code == 401


def test_empty_selection_rejected(bridge):
    with pytest.raises(HTTPError) as exc:
        urlopen(
            request(
                bridge,
                path="/v1/selection",
                method="POST",
                payload={"photos": []},
            ),
            timeout=2,
        )
    assert exc.value.code == 400


def test_expired_selection_is_not_returned(bridge, monkeypatch):
    payload = {"photos": [{"fileName": "OLD.CR3"}]}
    with urlopen(
        request(bridge, path="/v1/selection", method="POST", payload=payload),
        timeout=2,
    ):
        pass
    monkeypatch.setattr(
        "mhphotos_lightroom_mcp.local_bridge.time.monotonic",
        lambda: bridge.selection_updated_at + 301,
    )
    with pytest.raises(HTTPError) as exc:
        urlopen(request(bridge, path="/v1/selection"), timeout=2)
    assert exc.value.code == 404
    with urlopen(request(bridge), timeout=2) as response:
        assert json.load(response)["selection_received"] is False


def test_short_token_rejected():
    with pytest.raises(ValueError):
        make_server("too-short")

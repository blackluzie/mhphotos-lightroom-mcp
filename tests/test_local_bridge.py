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


def test_selection_cannot_be_read_with_invalid_token(bridge):
    payload = {"photos": [{"fileName": "PRIVATE.CR3"}]}
    with urlopen(
        request(bridge, path="/v1/selection", method="POST", payload=payload),
        timeout=2,
    ):
        pass
    with pytest.raises(HTTPError) as exc:
        urlopen(
            request(bridge, token="invalid", path="/v1/selection"),
            timeout=2,
        )
    assert exc.value.code == 401
    with urlopen(request(bridge, path="/v1/selection"), timeout=2) as response:
        assert json.load(response) == payload


def test_invalid_token_is_rejected(bridge):
    with pytest.raises(HTTPError) as exc:
        urlopen(request(bridge, token="wrong"), timeout=2)
    assert exc.value.code == 401


def test_unknown_route_is_rejected(bridge):
    with pytest.raises(HTTPError) as exc:
        urlopen(request(bridge, path="/v1/photos"), timeout=2)
    assert exc.value.code == 404


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_unsupported_methods_cannot_change_selection(bridge, method):
    original = {"photos": [{"fileName": "KEEP.CR3"}]}
    with urlopen(
        request(bridge, path="/v1/selection", method="POST", payload=original),
        timeout=2,
    ):
        pass
    with pytest.raises(HTTPError) as exc:
        urlopen(
            request(
                bridge,
                path="/v1/selection",
                method=method,
                payload={"photos": [{"fileName": "REPLACE.CR3"}]},
            ),
            timeout=2,
        )
    assert exc.value.code == 501
    with urlopen(request(bridge, path="/v1/selection"), timeout=2) as response:
        assert json.load(response) == original


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


@pytest.mark.parametrize(
    "payload",
    [
        {"photos": [{"fileName": "IMG.CR3"}] * 6},
        {"photos": [{"fileName": "x" * 81}]},
        {"photos": [{"rating": True}]},
        {"photos": [{"rating": 3.5}]},
        {"photos": [{"fileName": ["IMG.CR3"]}]},
    ],
)
def test_invalid_selection_values_rejected(bridge, payload):
    with pytest.raises(HTTPError) as exc:
        urlopen(
            request(
                bridge,
                path="/v1/selection",
                method="POST",
                payload=payload,
            ),
            timeout=2,
        )
    assert exc.value.code == 400


def test_invalid_submission_preserves_previous_selection(bridge):
    original = {"photos": [{"fileName": "VALID.CR3", "rating": 5}]}
    with urlopen(
        request(bridge, path="/v1/selection", method="POST", payload=original),
        timeout=2,
    ):
        pass

    with pytest.raises(HTTPError) as exc:
        urlopen(
            request(
                bridge,
                path="/v1/selection",
                method="POST",
                payload={"photos": [{"absolutePath": "/private/secret.CR3"}]},
            ),
            timeout=2,
        )
    assert exc.value.code == 400

    with urlopen(request(bridge, path="/v1/selection"), timeout=2) as response:
        assert json.load(response) == original


def test_oversized_request_body_rejected(bridge):
    req = Request(
        f"http://127.0.0.1:{bridge.server_port}/v1/selection",
        data=b"x" * 8193,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with pytest.raises(HTTPError) as exc:
        urlopen(req, timeout=2)
    assert exc.value.code == 413
    with urlopen(request(bridge), timeout=2) as response:
        assert json.load(response)["selection_received"] is False


def test_non_json_content_type_rejected(bridge):
    req = Request(
        f"http://127.0.0.1:{bridge.server_port}/v1/selection",
        data=b'{"photos":[{"fileName":"TEST.CR3"}]}',
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "text/plain",
        },
        method="POST",
    )
    with pytest.raises(HTTPError) as exc:
        urlopen(req, timeout=2)
    assert exc.value.code == 415


def test_malformed_json_rejected(bridge):
    req = Request(
        f"http://127.0.0.1:{bridge.server_port}/v1/selection",
        data=b'{"photos":[',
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with pytest.raises(HTTPError) as exc:
        urlopen(req, timeout=2)
    assert exc.value.code == 400


def test_empty_photo_record_rejected(bridge):
    with pytest.raises(HTTPError) as exc:
        urlopen(
            request(
                bridge,
                path="/v1/selection",
                method="POST",
                payload={"photos": [{}]},
            ),
            timeout=2,
        )
    assert exc.value.code == 400


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


def test_foreign_origin_is_rejected(bridge):
    req = request(
        bridge,
        path="/v1/selection",
        method="POST",
        payload={"photos": [{"fileName": "SAMPLE.CR3"}]},
    )
    req.add_header("Origin", "https://untrusted.example")
    with pytest.raises(HTTPError) as exc:
        urlopen(req, timeout=2)
    assert exc.value.code == 403


def test_same_origin_post_is_accepted(bridge):
    req = request(
        bridge,
        path="/v1/selection",
        method="POST",
        payload={"photos": [{"fileName": "SAMPLE.CR3"}]},
    )
    req.add_header("Origin", f"http://127.0.0.1:{bridge.server_port}")
    with urlopen(req, timeout=2) as response:
        assert json.load(response)["accepted"] == 1


def test_status_check_clears_expired_selection(bridge, monkeypatch):
    payload = {"photos": [{"fileName": "STALE.CR3"}]}
    with urlopen(
        request(bridge, path="/v1/selection", method="POST", payload=payload),
        timeout=2,
    ):
        pass
    monkeypatch.setattr(
        "mhphotos_lightroom_mcp.local_bridge.time.monotonic",
        lambda: bridge.selection_updated_at + 301,
    )
    with urlopen(request(bridge), timeout=2) as response:
        assert json.load(response)["selection_received"] is False
    assert bridge.selection is None


def test_short_token_rejected():
    with pytest.raises(ValueError):
        make_server("too-short")

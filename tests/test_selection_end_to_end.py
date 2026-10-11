"""Exercise the local HTTP-to-MCP metadata snapshot path."""

import json
import threading
from urllib.request import Request, urlopen

from mhphotos_lightroom_mcp.bridge import BridgeConfig
from mhphotos_lightroom_mcp.local_bridge import make_server
from mhphotos_lightroom_mcp.server import selected_photo_metadata


def test_uploaded_selection_is_available_to_mcp(monkeypatch):
    token = "test-token-for-local-integration-123456789"
    server = make_server(token, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        monkeypatch.setenv("MHPHOTOS_BRIDGE_TOKEN", token)
        monkeypatch.setattr(
            "mhphotos_lightroom_mcp.server.BridgeConfig",
            lambda token: BridgeConfig(port=server.server_port, token=token),
        )
        photos = [{"fileName": "SAMPLE.CR3", "rating": "5"}]
        request = Request(
            f"http://127.0.0.1:{server.server_port}/v1/selection",
            data=json.dumps({"photos": photos}).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=2) as response:
            assert json.load(response)["accepted"] == 1
        assert selected_photo_metadata() == {
            "photos": photos,
            "source": "local_bridge_snapshot",
        }
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)

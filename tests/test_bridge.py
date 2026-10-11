from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from mhphotos_lightroom_mcp.bridge import BridgeConfig, read_status


def test_missing_token_does_not_connect():
    assert read_status(BridgeConfig()) == {"connected": False, "reason": "missing_token"}


def test_invalid_port_rejected():
    with pytest.raises(ValueError):
        BridgeConfig(port=0)


def test_unavailable_bridge_is_reported():
    with patch("mhphotos_lightroom_mcp.bridge.urlopen", side_effect=OSError("offline")):
        assert read_status(BridgeConfig(token="test-token")) == {
            "connected": False,
            "reason": "bridge_unavailable",
        }


def test_invalid_bridge_token_is_reported():
    error = HTTPError("http://127.0.0.1:8765/v1/status", 401, "Unauthorized", {}, None)
    with patch("mhphotos_lightroom_mcp.bridge.urlopen", side_effect=error):
        assert read_status(BridgeConfig(token="wrong-token")) == {
            "connected": False,
            "reason": "unauthorized",
        }

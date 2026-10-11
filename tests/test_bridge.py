from unittest.mock import patch

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

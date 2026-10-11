from unittest.mock import patch

from mhphotos_lightroom_mcp.server import connection_status


def test_status_without_bridge_configuration(monkeypatch):
    monkeypatch.delenv("MHPHOTOS_BRIDGE_TOKEN", raising=False)
    result = connection_status()
    assert result["connected"] is False
    assert "not configured" in result["message"]


def test_status_when_local_bridge_is_running(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_BRIDGE_TOKEN", "test-token")
    with patch(
        "mhphotos_lightroom_mcp.server.read_status",
        return_value={"bridge_running": True, "connected": False},
    ):
        result = connection_status()
    assert result["connected"] is False
    assert "not connected" in result["message"]


def test_status_when_bridge_is_unavailable(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_BRIDGE_TOKEN", "test-token")
    with patch(
        "mhphotos_lightroom_mcp.server.read_status",
        return_value={"connected": False, "reason": "bridge_unavailable"},
    ):
        result = connection_status()
    assert result["connected"] is False
    assert "unavailable" in result["message"]

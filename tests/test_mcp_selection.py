from unittest.mock import patch

from mhphotos_lightroom_mcp.server import selected_photo_metadata


def test_selection_requires_bridge_token(monkeypatch):
    monkeypatch.delenv("MHPHOTOS_BRIDGE_TOKEN", raising=False)
    assert selected_photo_metadata() == {
        "photos": [],
        "reason": "bridge_not_configured",
    }


def test_selection_returns_bridge_snapshot(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_BRIDGE_TOKEN", "test-token")
    photos = [{"fileName": "IMG_1000.CR3", "rating": 5}]
    with patch(
        "mhphotos_lightroom_mcp.server.read_selection",
        return_value={"photos": photos},
    ):
        assert selected_photo_metadata() == {
            "photos": photos,
            "source": "local_bridge_snapshot",
        }


def test_selection_handles_bridge_unavailable(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_BRIDGE_TOKEN", "test-token")
    with patch(
        "mhphotos_lightroom_mcp.server.read_selection",
        return_value={"reason": "bridge_unavailable"},
    ):
        assert selected_photo_metadata() == {
            "photos": [],
            "reason": "bridge_unavailable",
        }

from mhphotos_lightroom_mcp.server import connection_status


def test_disconnected_by_default():
    result = connection_status()
    assert result["connected"] is False
    assert result["mode"] == "prototype"

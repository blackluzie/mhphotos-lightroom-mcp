import pytest
from mhphotos_lightroom_mcp.desktop import read_bridge_token


def test_token_file_normalized_without_disclosure(tmp_path):
    path = tmp_path / "token"
    path.write_text("a" * 64 + "\n")
    path.chmod(0o600)
    assert read_bridge_token(path) == "a" * 64
    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode 600"):
        read_bridge_token(path)
    path.chmod(0o600)
    path.write_text("a" * 32 + " " + "b" * 32)
    with pytest.raises(ValueError, match="format"):
        read_bridge_token(path)

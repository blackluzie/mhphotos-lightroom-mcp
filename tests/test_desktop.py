import os

import pytest
from mhphotos_lightroom_mcp.desktop import read_bridge_token
from mhphotos_lightroom_mcp.private_files import windows_acl


def test_token_file_normalized_without_disclosure(tmp_path):
    path = tmp_path.resolve() / "token"
    path.write_text("a" * 64 + "\n")
    path.chmod(0o600)
    if os.name == "nt":
        windows_acl(path, protect=True)
    assert read_bridge_token(path) == "a" * 64
    if os.name != "nt":
        path.chmod(0o644)
        with pytest.raises(PermissionError, match="700/600"):
            read_bridge_token(path)
    path.chmod(0o600)
    path.write_text("a" * 32 + " " + "b" * 32)
    with pytest.raises(ValueError, match="format"):
        read_bridge_token(path)

"""Optional private test profile; default desktop behaviour is unchanged."""

import json
import os
from pathlib import Path

from mhphotos_lightroom_mcp.private_files import validate_private


def profile_root() -> Path | None:
    value = os.environ.get("MHPHOTOS_PROFILE_ROOT")
    if value is None:
        return None
    root = Path(value)
    if not root.is_absolute():
        raise ValueError("Profile path must be absolute")
    validate_private(root, directory=True)
    return root


def bridge_port() -> int:
    root = profile_root()
    if root is None:
        return 8765
    path = root / "profile.json"
    validate_private(path)
    if path.stat().st_size > 1024:
        raise ValueError("Profile config too large")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {"port"}:
        raise ValueError("Invalid profile configuration")
    port = data["port"]
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not 1024 <= port <= 65535
        or port == 8765
    ):
        raise ValueError("Test profile requires a separate unprivileged port")
    return port


def bridge_token_path() -> Path:
    root = profile_root()
    return (
        root / "bridge-token"
        if root is not None
        else Path.home() / ".mhphotos-lightroom-token"
    )


def transfer_path() -> Path:
    root = profile_root()
    return (
        root / "transfers"
        if root is not None
        else Path.home() / ".mhphotos-lightroom/transfers"
    )


def bridge_url(route: str) -> str:
    if route not in {"selection", "collections"}:
        raise ValueError("Invalid local route")
    return f"http://127.0.0.1:{bridge_port()}/v1/{route}"

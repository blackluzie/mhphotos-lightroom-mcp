"""Secret-free launchd entry point for the local desktop services."""

import json
import os
import stat
import sys
from pathlib import Path


def read_bridge_token(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Bridge token file requires owner-only mode 600")
        token = handle.read(4096).strip()
    if not 32 <= len(token) <= 1024 or any(char.isspace() for char in token):
        raise ValueError("Invalid bridge token file format")
    return token


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"bridge", "http"}:
        raise ValueError("Specify bridge or http")
    os.environ["MHPHOTOS_BRIDGE_TOKEN"] = read_bridge_token(
        Path.home() / ".mhphotos-lightroom-token"
    )
    if sys.argv[1] == "bridge":
        from mhphotos_lightroom_mcp.local_bridge import main as run
    else:
        config_path = Path.home() / ".config/mhphotos-lightroom/desktop.json"
        config = json.loads(config_path.read_text())
        os.environ["MHPHOTOS_PUBLIC_URL"] = config["public_url"]
        os.environ["MHPHOTOS_CHATGPT_REDIRECT_URIS"] = " ".join(config["redirect_uris"])
        os.environ["MHPHOTOS_MCP_TRANSPORT"] = "streamable-http"
        from mhphotos_lightroom_mcp.server import main as run
    run()


if __name__ == "__main__":
    main()

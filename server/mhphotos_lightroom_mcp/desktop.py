"""Secret-free launchd entry point for the local desktop services."""

import json
import os
import sys
from pathlib import Path

from mhphotos_lightroom_mcp.pairing import read_bridge_token


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

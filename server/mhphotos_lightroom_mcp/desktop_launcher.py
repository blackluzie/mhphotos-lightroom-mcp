"""Desktop entry point for MHPhotos Lightroom."""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from mhphotos_lightroom_mcp.desktop import read_bridge_token

BRIDGE_URL = "http://127.0.0.1:8765/v1/selection"


def bridge_status():
    """Check the authenticated local bridge without exposing credentials."""
    try:
        token = read_bridge_token(
            Path.home() / ".mhphotos-lightroom-token"
        )

        request = urllib.request.Request(
            BRIDGE_URL,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )

        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            json.load(response)
            return "connected"

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # A reachable bridge may have no current selection.
            return "connected_no_selection"
        return "http_error"

    except (
        OSError,
        ValueError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ):
        return "unavailable"


def platform_info():
    return {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "home_available": Path.home().is_dir(),
        "token_file_exists": (
            Path.home() / ".mhphotos-lightroom-token"
        ).is_file(),
        "bridge": bridge_status(),
    }


def main():
    if sys.argv[1:] == ["--status"]:
        print(json.dumps(platform_info()))
        return

    from mhphotos_lightroom_mcp.selection_sender import main as send_selection
    send_selection()


if __name__ == "__main__":
    main()

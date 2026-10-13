"""Send explicitly selected Lightroom metadata to the local bridge."""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from mhphotos_lightroom_mcp.desktop import read_bridge_token
from mhphotos_lightroom_mcp.selection_transport import (
    read_transfer,
    transport_directory,
)

BRIDGE_URL = "http://127.0.0.1:8765/v1/selection"
MAX_PHOTOS = 5
ALLOWED_FIELDS = {"fileName", "rating", "pickStatus"}


def send_selection(payload: dict) -> dict:
    photos = payload.get("photos")

    if not isinstance(photos, list) or not 1 <= len(photos) <= MAX_PHOTOS:
        raise ValueError("Expected between 1 and 5 photos")

    for photo in photos:
        if not isinstance(photo, dict) or set(photo) != ALLOWED_FIELDS:
            raise ValueError("Invalid photo fields")

        if not all(
            isinstance(value, (str, int))
            and not isinstance(value, bool)
            and len(str(value)) <= 80
            for value in photo.values()
        ):
            raise ValueError("Invalid metadata value")

    token = read_bridge_token(
        Path.home() / ".mhphotos-lightroom-token"
    )

    request = urllib.request.Request(
        BRIDGE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
        return json.load(response)


def main() -> None:
    try:
        if len(sys.argv) == 1:
            payload = json.load(sys.stdin)

        elif len(sys.argv) == 2:
            input_path = Path(sys.argv[1])

            directory = transport_directory()

            if (
                input_path.parent != directory
                or not input_path.name.startswith("selection-")
                or not input_path.name.endswith(".json")
            ):
                raise ValueError("Invalid transfer path")

            payload = read_transfer(input_path)

            try:
                result = send_selection(payload)
                print(json.dumps(result))
            finally:
                input_path.unlink(missing_ok=True)

            return

        else:
            raise ValueError("Invalid arguments")

        result = send_selection(payload)
        print(json.dumps(result))

    except (ValueError, OSError, urllib.error.URLError, json.JSONDecodeError):
        print(json.dumps({"error": "selection_transfer_failed"}))
        sys.exit(1)


if __name__ == "__main__":
    main()

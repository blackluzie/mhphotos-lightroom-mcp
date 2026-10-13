"""Send a user-approved collection overview to the private loopback bridge."""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from mhphotos_lightroom_mcp.collection_schema import (
    MAX_COLLECTION_BODY,
    MAX_COLLECTIONS,
    validate_collections,
)
from mhphotos_lightroom_mcp.pairing import read_bridge_token
from mhphotos_lightroom_mcp.runtime_profile import bridge_token_path, bridge_url
from mhphotos_lightroom_mcp.selection_transport import read_transfer

BRIDGE_URL = bridge_url("collections")


def send_collections(payload: dict) -> dict:
    records = validate_collections(payload)
    data = json.dumps(payload).encode("utf-8")
    if len(data) > MAX_COLLECTION_BODY:
        raise ValueError("Collection overview too large")
    token = read_bridge_token(bridge_token_path())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        BRIDGE_URL.rsplit("/", 1)[0] + "/capabilities",
        headers={"Authorization": f"Bearer {token}"},
    )
    with opener.open(request, timeout=5) as response:
        capabilities = json.load(response)
    if (
        not isinstance(capabilities, dict)
        or capabilities.get("collection_overview") is not True
        or capabilities.get("max_collections") != MAX_COLLECTIONS
    ):
        raise ValueError("Bridge does not support collection overview")
    request = urllib.request.Request(
        BRIDGE_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with opener.open(request, timeout=5) as response:
        result = json.load(response)
    if (
        not isinstance(result, dict)
        or isinstance(result.get("accepted"), bool)
        or result.get("accepted") != len(records)
    ):
        raise ValueError("Invalid collection acknowledgement")
    return result


def main() -> None:
    try:
        if len(sys.argv) != 2:
            raise ValueError("Expected private transfer file")
        path = Path(sys.argv[1])
        payload = read_transfer(path, max_bytes=MAX_COLLECTION_BODY)
        try:
            result = send_collections(payload)
            print(json.dumps(result))
        finally:
            path.unlink(missing_ok=True)
    except (ValueError, OSError, urllib.error.URLError):
        print(json.dumps({"error": "collection_transfer_failed"}))
        sys.exit(1)


if __name__ == "__main__":
    main()

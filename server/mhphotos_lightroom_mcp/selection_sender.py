"""Send explicitly selected Lightroom metadata to the local bridge."""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from mhphotos_lightroom_mcp.desktop import read_bridge_token
from mhphotos_lightroom_mcp.runtime_profile import bridge_token_path, bridge_url
from mhphotos_lightroom_mcp.selection_schema import (
    BASE_FIELDS,
    MAX_BODY,
    validate_selection,
)
from mhphotos_lightroom_mcp.selection_transport import (
    read_transfer,
    transport_directory,
)

BRIDGE_URL = bridge_url("selection")


def send_selection(payload: dict) -> dict:
    photos = validate_selection(payload, require_base=True)
    data = json.dumps(payload).encode("utf-8")
    if len(data) > MAX_BODY:
        raise ValueError("Transfer payload too large")

    token = read_bridge_token(bridge_token_path())

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # Negotiate only when optional metadata is present. Old bridges return 404
    # for capabilities: retain the original three fields and disclose fallback.
    metadata_mode = "basic"
    if len(photos) > 5 or any(set(photo) - BASE_FIELDS for photo in photos):
        capabilities_request = urllib.request.Request(
            BRIDGE_URL.rsplit("/", 1)[0] + "/capabilities",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with opener.open(capabilities_request, timeout=5) as response:
                capabilities = json.load(response)
            if (
                not isinstance(capabilities, dict)
                or capabilities.get("protocol_version") != 2
                or not isinstance(capabilities.get("metadata_fields"), list)
                or not all(
                    isinstance(field, str) for field in capabilities["metadata_fields"]
                )
            ):
                raise ValueError("Invalid bridge capabilities")
            supported = set(capabilities["metadata_fields"])
            limit = capabilities.get("max_photos")
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or len(photos) > limit
            ):
                raise ValueError("Bridge selection limit exceeded")
            if not all(set(photo) <= supported for photo in photos):
                raise ValueError("Bridge does not support requested metadata")
            metadata_mode = "extended"
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            if len(photos) > 5:
                raise ValueError(
                    "Legacy bridge supports at most five photos; update the bridge first"
                ) from error
            payload = {
                "photos": [
                    {key: value for key, value in photo.items() if key in BASE_FIELDS}
                    for photo in photos
                ]
            }
            data = json.dumps(payload).encode("utf-8")
            metadata_mode = "basic_legacy_bridge"

    request = urllib.request.Request(
        BRIDGE_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    with opener.open(request, timeout=5) as response:
        result = json.load(response)
    # Preserve the legacy response shape for basic selections.
    if metadata_mode != "basic":
        result["metadata_mode"] = metadata_mode
    return result


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

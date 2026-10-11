"""Local read-only Lightroom bridge transport.

Only loopback connections are accepted. The shared token is required for
requests; the bridge does not access the Lightroom catalog directly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class BridgeConfig:
    port: int = 8765
    token: str = ""

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("Invalid bridge port")


def read_status(config: BridgeConfig) -> dict:
    """Query a locally running bridge; never contact a remote host."""
    if not config.token:
        return {"connected": False, "reason": "missing_token"}
    request = Request(
        f"http://127.0.0.1:{config.port}/v1/status",
        headers={"Authorization": f"Bearer {config.token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=2) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        return {"connected": False, "reason": "bridge_unavailable"}
    if not isinstance(payload, dict):
        return {"connected": False, "reason": "invalid_response"}
    return payload

"""Local read-only Lightroom bridge transport.

Only loopback connections are accepted. The shared token is required for
requests; the bridge does not access the Lightroom catalog directly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from mhphotos_lightroom_mcp.runtime_profile import bridge_port

# Local bearer credentials must never traverse an environment-configured proxy.
urlopen = build_opener(ProxyHandler({})).open


@dataclass(frozen=True)
class BridgeConfig:
    port: int = field(default_factory=bridge_port)
    token: str = ""

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("Invalid bridge port")


def _get(config: BridgeConfig, path: str) -> dict:
    if not config.token:
        return {"connected": False, "reason": "missing_token"}
    request = Request(
        f"http://127.0.0.1:{config.port}{path}",
        headers={"Authorization": f"Bearer {config.token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=2) as response:
            payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 401:
            return {"connected": False, "reason": "unauthorized"}
        if exc.code == 404 and path == "/v1/collections":
            return {"collections": [], "reason": "no_collections"}
        if exc.code == 404 and path == "/v1/selection":
            return {"photos": [], "reason": "no_selection"}
        return {"connected": False, "reason": "bridge_unavailable"}
    except (URLError, TimeoutError, ValueError, OSError):
        return {"connected": False, "reason": "bridge_unavailable"}
    if not isinstance(payload, dict):
        return {"connected": False, "reason": "invalid_response"}
    return payload


def read_status(config: BridgeConfig) -> dict:
    """Query a locally running bridge; never contact a remote host."""
    return _get(config, "/v1/status")


def read_selection(config: BridgeConfig) -> dict:
    """Read last submitted selection from the local in-memory bridge."""
    return _get(config, "/v1/selection")


def read_collections(config: BridgeConfig) -> dict:
    """Read only the user-submitted overview, never enumerate a catalog."""
    return _get(config, "/v1/collections")

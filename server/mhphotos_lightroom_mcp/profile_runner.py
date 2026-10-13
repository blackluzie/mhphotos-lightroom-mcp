"""Launch only a private side-by-side profile; no service registration or public HTTP."""

import json
import os
import sys


def main() -> None:
    if len(sys.argv) not in {3, 4} or sys.argv[2] not in {
        "bridge",
        "send",
        "collections",
        "status",
        "mcp",
        "poll",
        "reply",
        "worker-stop",
    }:
        raise ValueError("Expected profile root and mode")
    mode = sys.argv[2]
    if len(sys.argv) != (4 if mode in {"send", "collections", "poll", "reply"} else 3):
        raise ValueError("Invalid profile arguments")
    os.environ["MHPHOTOS_PROFILE_ROOT"] = sys.argv[1]
    from mhphotos_lightroom_mcp.pairing import read_bridge_token
    from mhphotos_lightroom_mcp.runtime_profile import bridge_port, bridge_token_path

    port = bridge_port()  # Validate config before any listener or file submission.
    token = read_bridge_token(bridge_token_path())
    if mode == "bridge":
        from mhphotos_lightroom_mcp.local_bridge import make_server

        server = make_server(token, port=port, live_reads=True)
        print(f"MHPhotos Mac test bridge on 127.0.0.1:{port}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    elif mode == "worker-stop":
        from mhphotos_lightroom_mcp.live_worker_io import post_live

        post_live("stop", {})
    elif mode in {"poll", "reply"}:
        from mhphotos_lightroom_mcp.live_worker_io import main as run

        sys.argv = [sys.argv[0], mode, sys.argv[3]]
        run()
    elif mode == "status":
        from mhphotos_lightroom_mcp.bridge import BridgeConfig, read_status

        print(json.dumps(read_status(BridgeConfig(port=port, token=token))))
    elif mode == "mcp":
        os.environ["MHPHOTOS_BRIDGE_TOKEN"] = token
        os.environ["MHPHOTOS_MCP_TRANSPORT"] = "stdio"
        from mhphotos_lightroom_mcp.server import main as run

        run()
    else:
        if mode == "send":
            from mhphotos_lightroom_mcp import selection_sender
            from mhphotos_lightroom_mcp.runtime_profile import bridge_url

            selection_sender.BRIDGE_URL = bridge_url("selection")
            run = selection_sender.main
        else:
            from mhphotos_lightroom_mcp import collection_sender
            from mhphotos_lightroom_mcp.runtime_profile import bridge_url

            collection_sender.BRIDGE_URL = bridge_url("collections")
            run = collection_sender.main
        sys.argv = [sys.argv[0], sys.argv[3]]
        run()


if __name__ == "__main__":
    main()

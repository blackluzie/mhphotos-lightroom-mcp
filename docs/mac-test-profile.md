# Separate Mac test profile

The existing installation stays on port 8765. A test profile has a different,
private root, token, transfer directory and port (for example 8767). Do not put
runtime environments or credentials in an iCloud-synced development folder;
use a private local Application Support directory. Recreate Python virtual
environments at their final path rather than moving them.

## Profile contract

The private root must have owner-only directory permissions. It contains:

- `profile.json`, owner-only: exactly `{"port":8767}`.
- `bridge-token`, owner-only: provision with `ensure_bridge_token` at this path.
- `transfers/`, owner-only directory.

Relative roots, symbolic links, malformed configs, privileged ports, port 8765
and configurable remote hosts are refused. All connections use IPv4 loopback.
Without `MHPHOTOS_PROFILE_ROOT`, the existing token path, transfer path and port
remain unchanged. The profile runner validates config before any listener/send.

Use the **installed environment's Python** with:

```sh
python -I -m mhphotos_lightroom_mcp.profile_runner /absolute/private/root bridge
python -I -m mhphotos_lightroom_mcp.profile_runner /absolute/private/root status
python -I -m mhphotos_lightroom_mcp.profile_runner /absolute/private/root mcp
```

The `mcp` mode is local stdio only. It does not start a public HTTP endpoint or
modify an existing OAuth connection. The `send` and `collections` modes take one
private transfer filename after the mode. Transfer validation refuses files from
outside the profile. Profile credentials are never passed as command arguments.
The runner does not register a service; any registration is explicit and uses a
different service label from the existing production bridge.

## Test plugin

A separately copied plugin must use a unique `LrToolkitIdentifier`, plugin name
and menu labels. Its private `Installation.lua` contains only the installed
Python path and `profileRoot`, no token. The two transfer commands route through
the profile runner when `profileRoot` is present. Mac-only profile launchers
refuse Windows rather than constructing an unverified command line.

The current user-specific test plugin is named **MHPhotos Mac Test** and is
separate from the existing MHPhotos Lightroom MCP plugin. Its transfer data does
not appear through the existing production connector; inspect it through the
profile's local stdio MCP server. Actual Lightroom plugin execution remains a
manual integration check when native UI access is unavailable.

## Tests

Tests cover unchanged default behaviour, private paths, transfer separation,
config/port/host rejection and the actual Lua selection command routing. The
installed runtime was also tested end to end with a real test bridge and stdio
MCP client, 50 extended photo records and a collection hierarchy, without using
production port 8765. Ratings and all catalog/collection write operations remain
disabled.

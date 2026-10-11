# Integration verification plan

The current Python bridge and Lightroom plugin are prototypes. A green Python CI run does not prove Lightroom SDK compatibility.

## Before testing on macOS

1. Create a dedicated Lightroom Classic test catalog with non-sensitive test photos.
2. Generate a unique local token and keep it out of the repository and logs.
3. Run the Python localhost bridge and MCP server with the same token.
4. Install the Lua plugin using Lightroom Classic Plug-in Manager.
5. Preview selected metadata locally first; confirm the menu and SDK calls work.
6. Explicitly send one test selection using the plugin menu.
7. Query the MCP selected-photo tool and compare the returned allowlisted fields.
8. Confirm unauthenticated requests fail, no network listener is exposed beyond loopback, and no catalog or original file is modified.

## Current limitations

- Lua has not been executed inside Lightroom Classic.
- Token entry and HTTP transfer must be validated against the installed Lightroom SDK.
- Selection data is a snapshot, not a live Lightroom query.
- Never use the production catalog for the initial test.

## Troubleshooting checklist for the first Lightroom run

- **Plugin missing:** Confirm the `.lrplugin` folder (not its parent) was added in Plug-in Manager, then check the Library Plug-in Extras menu.
- **Preview fails:** Capture the exact Lightroom dialog text and check the Plug-in Manager status; this points to Lua SDK/menu compatibility rather than the Python bridge.
- **Token rejected:** Use the exact same token in the Python bridge process and the Lightroom send dialog. Do not paste tokens into issue reports or screenshots.
- **Transfer fails:** Confirm the bridge is listening on `127.0.0.1:8765` on the same Mac. Verify the Python bridge is still running and check for port conflicts.
- **More than five photos:** Reduce the selection to one to five images. The plugin intentionally refuses larger selections.
- **MCP reports no selection:** Submit again; snapshots expire after five minutes. Also confirm the MCP server uses the same token.
- **MCP reports unavailable:** Distinguish a bridge process that is not running from a token mismatch; neither implies Lightroom catalog damage.
- **Unexpected data:** Stop testing and inspect the allowlisted metadata fields. Do not use real client photos while investigating.

## Acceptance criteria before marking PR ready

1. Python CI is green at the final commit.
2. The plugin loads without an SDK or syntax error in Lightroom Classic on macOS.
3. Metadata preview shows the intended selected test photo.
4. A user-confirmed send transfers only the allowed fields and the MCP tool reads the same snapshot.
5. Invalid tokens, oversized selections, and absent bridge process fail safely.
6. No catalog changes, photo modifications, or external network connections are observed.

Do not merge on Python CI alone: the Lua plugin cannot be executed by that workflow.

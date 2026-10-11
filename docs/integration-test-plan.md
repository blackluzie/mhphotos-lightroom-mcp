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

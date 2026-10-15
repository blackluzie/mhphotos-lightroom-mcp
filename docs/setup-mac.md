# Setup on a Mac

You need: Lightroom Classic, Claude Code (or another MCP client), and [uv](https://docs.astral.sh/uv/)
(`brew install uv`). Use a **disposable catalog** (File > New Catalog, import about 10 photos) for the first run.

> The `uvx --from git+https://...` commands need read access to the repository. If it is private, clone it and use
> `uvx --from /path/to/mhphotos-lightroom-mcp mhphotos-lightroom-mcp ...` or `pip install -e .` instead.

## 1. Install the Lightroom plugin
```bash
uvx --from git+https://github.com/blackluzie/mhphotos-lightroom-mcp mhphotos-lightroom-mcp install-plugin
```
It copies the plugin to `~/Documents/MHPhotos-Lightroom-Plugin/mhphotos.lrdevplugin` (pass another folder as
argument to change that). In Lightroom: **File > Plug-in Manager > Add**, select that folder.

## 2. Register the MCP server
```bash
claude mcp add mhphotos-lightroom --env MHPHOTOS_ADAPTER=bridge -- \
  uvx --from git+https://github.com/blackluzie/mhphotos-lightroom-mcp mhphotos-lightroom-mcp
```
Reads only for now. Start a new Claude Code session, then ask: *"Run lightroom_selftest"*. It checks every read
command on a few photos and reports each error text. Send failures to the maintainer; most first-run problems
are small API mismatches. In Lightroom the same check is **Library > Plug-in Extras > MHPhotos MCP: Selbsttest**.

`mhphotos-lightroom-mcp doctor` in a terminal shows what is set up, what is missing and whether the plugin is
connected (never prints secrets).

## 3. Allow changes (only when the self test is green)
Add environment variables to the server, for example:
```bash
claude mcp remove mhphotos-lightroom
claude mcp add mhphotos-lightroom \
  --env MHPHOTOS_ADAPTER=bridge --env MHPHOTOS_ALLOW_WRITES=1 \
  --env MHPHOTOS_EXPORT_DIR="$HOME/Pictures/MHPhotos-Export" -- \
  uvx --from git+https://github.com/blackluzie/mhphotos-lightroom-mcp mhphotos-lightroom-mcp
```
Keep `MHPHOTOS_CONFIRMATION` at its default (`ask`). A client that supports MCP elicitation then asks you yes/no
before each change; a client that does not falls back to `confirm=true` alone. If you want the server to refuse
in that case instead, set `MHPHOTOS_CONFIRMATION=require`. Test it once with a harmless rating change.

## 4. Connect the MHPhotos site (optional)
1. Store the admin token of the MHPhotos app in the Keychain (it is the token you use to sign in to the
   MHPhotos admin area; the app checks it in the header `X-Admin-Token`):
   `security add-generic-password -s mhphotos-admin-token -a "$USER" -w` (you are prompted for the value).
2. Add `--env MHPHOTOS_INBOX_URL=https://<your MHPhotos domain>`.
3. For going live add `MHPHOTOS_ALLOW_PUBLISH=1`, for Instagram scheduling `MHPHOTOS_ALLOW_INSTAGRAM=1`.

Check with `doctor`: it says whether the app is reachable and the token accepted.

### If a sync service already runs on the Mac
If you already run a service that moves Lightroom exports into MHPhotos, it has to authenticate against the same
MHPhotos admin endpoints, so it most likely holds an admin token already (check that service's configuration;
the source of that service is not part of this repository, so this is an assumption). You can put the same token
into the Keychain as above. This project does not need that service; it talks to the MHPhotos API directly. If you
prefer to keep one path, export into the service's watch folder with `MHPHOTOS_EXPORT_DIR` and use only the read
and publish tools.

## Updating and troubleshooting
- `uvx` caches by version; force a refresh with `uvx --refresh --from git+... mhphotos-lightroom-mcp doctor`.
- Plugin shows "unreachable": the MCP server is not running yet (it starts with your MCP client).
- Plugin shows "unauthorized": the server restarted with a new token; the plugin re-reads it within seconds.
- Everything is logged locally in `~/.mhphotos-lightroom-mcp/audit.log`; ask Claude for `list_changes`.

# MHPhotos Lightroom MCP

Open-source Model Context Protocol server for Adobe Lightroom Classic. Ask Claude (or another MCP
client) to find, look at, rate, edit and publish your photos, with a safety gate in front of everything
that changes something.

> **Status:** reads, edits and the MHPhotos workflow are implemented and tested against a simulated
> Lightroom. The Lightroom plugin has had a **first run in real Lightroom Classic 15.6** (disposable catalog):
> self test 16/16, export, rating/flag/keywords with undo, colour labels, title/caption, virtual copies, collections,
> contact sheet and develop settings work. A server restart (new token) and a Lightroom restart reconnect by themselves. Not yet confirmed in real
> Lightroom: applying a preset made by `create_preset` (Lightroom does not list it; see [plugin/README.md](plugin/README.md)).
> Use a disposable catalog first and start with the plugin self test. Not affiliated with Adobe.

## Setup on a Mac in 5 steps (details: [docs/setup-mac.md](docs/setup-mac.md))
```bash
uvx --from git+https://github.com/blackluzie/mhphotos-lightroom-mcp mhphotos-lightroom-mcp install-plugin
# Lightroom Classic > File > Plug-in Manager > Add > ~/Documents/MHPhotos-Lightroom-Plugin/mhphotos.lrdevplugin
claude mcp add mhphotos-lightroom --env MHPHOTOS_ADAPTER=bridge -- \
  uvx --from git+https://github.com/blackluzie/mhphotos-lightroom-mcp mhphotos-lightroom-mcp
# then, in Claude:  "run lightroom_selftest"        (or in a terminal:  mhphotos-lightroom-mcp doctor)
```
From a clone instead: `pip install -e '.[dev]'` (then `mhphotos-lightroom-mcp ...`); the committed `.mcp.json` starts the server
in bridge mode with reads only.

## Tools
Everything is read-only unless noted. Changes are a **dry run** unless `confirm=true`, need an operator
opt-in (environment variable) and, when your client supports it, **you are asked directly** (yes/no).

| Group | Tools |
|-------|-------|
| Look & search | `connection_status`, `lightroom_selftest`, `list_collections` (`name_contains`), `list_photos` (`min_rating`, `year`/dates, `keyword`, `flag`, `location`, GPS circle), `get_photo_metadata`, `get_photo_previews` (images), `get_contact_sheet` (one numbered overview image), `get_develop_settings`, `list_presets`, `backlog_report`, `list_changes` |
| Organise (`MHPHOTOS_ALLOW_WRITES=1`) | `set_rating`, `set_flag`, `set_color_label`, `set_title_caption`, `add_keywords`, `create_collection`, `add_to_collection`, `undo_last_change` |
| Edit looks (`MHPHOTOS_ALLOW_WRITES=1`) | `apply_preset`, `apply_develop_settings`, `sync_develop_settings`, `create_preset`, `create_virtual_copies` |
| Export | `export_photos` (`MHPHOTOS_EXPORT_DIR`) |
| MHPhotos site | `mhphotos_galleries`, `mhphotos_inbox`, `mhphotos_inbox_images`, `mhphotos_inbox_contact_sheet`, `mhphotos_edit_preview` (basic edit before/after), `mhphotos_analyze_inbox`, `mhphotos_instagram_posts`, `mhphotos_instagram_images`, `send_to_mhphotos_inbox`, `mhphotos_publish` (public site, `MHPHOTOS_ALLOW_PUBLISH=1`), `mhphotos_schedule_instagram` / `mhphotos_instagram_queue` / `mhphotos_cancel_instagram` (social queue, `MHPHOTOS_ALLOW_INSTAGRAM=1`) |

The MHPhotos tools also run **without Lightroom and without an MCP client**: `mhphotos-lightroom-mcp call TOOL '{json}'`
(images are saved as files). That is how they are used from a cloud session on a phone; see [docs/kreuzfahrt.md](docs/kreuzfahrt.md).

Example requests and the exact tool sequence behind them: [docs/workflows.md](docs/workflows.md).

## Configuration (environment of the MCP server)
| Variable | Meaning |
|----------|---------|
| `MHPHOTOS_ADAPTER` | `mock` (default, sample data) or `bridge` (real Lightroom) |
| `MHPHOTOS_ALLOW_WRITES` | `1` enables changes to the catalog (see groups above) |
| `MHPHOTOS_EXPORT_DIR` | absolute folder; enables export, inbox upload and scheduling a Lightroom photo for Instagram |
| `MHPHOTOS_INBOX_URL` | https URL of the MHPhotos app (enables the MHPhotos tools) |
| `MHPHOTOS_ADMIN_TOKEN` / `MHPHOTOS_ADMIN_TOKEN_FILE` / macOS Keychain `mhphotos-admin-token` | admin token of the MHPhotos app, looked up in this order. Prefer the Keychain: `security add-generic-password -s mhphotos-admin-token -a "$USER" -w` |
| `MHPHOTOS_ALLOW_PUBLISH` | `1` allows `mhphotos_publish` (goes live on the public site) |
| `MHPHOTOS_ALLOW_INSTAGRAM` | `1` allows scheduling/cancelling Instagram posts |
| `MHPHOTOS_CONFIRMATION` | `ask` (default), `require` (refuse if the client cannot ask you), `flag` (never ask) |
| `MHPHOTOS_TOOLSETS` | e.g. `read,develop`; limits the offered tools (`all`, `read`, `edit`, `develop`, `mhphotos`) |
| `MHPHOTOS_BRIDGE_PORT`, `MHPHOTOS_BRIDGE_TOKEN`, `MHPHOTOS_CONFIG_DIR` | bridge port (47831), fixed token, config/audit folder (`~/.mhphotos-lightroom-mcp`) |

## Safety in short
- Loopback-only bridge with token; the plugin connects to the server, never the reverse.
- Reads need no opt-in. Changes: operator opt-in **and** `confirm=true` **and** (if supported) your yes/no.
- Every confirmed change is written to a local audit log (`list_changes`); rating, flag, color label,
  title/caption and keyword changes can be undone with `undo_last_change`.
- No deletes, no direct catalog database access, no secrets in the log or in tool results.
- Details: [docs/architecture.md](docs/architecture.md), [SECURITY.md](SECURITY.md).

## Repository
- `server/` Python MCP server, adapters, bridge, gate, audit, CLI (`doctor`, `install-plugin`)
- `plugin/` Lightroom Classic plugin (Lua) and its Lua tests
- `tests/` Python tests (tools, gate, bridge with a fake plugin, MHPhotos fake app, stdio end-to-end)
- `docs/` architecture, workflows, Mac setup, cloud API assessment

Lightroom Classic catalogs should be stored locally, even if originals live on mounted NAS volumes.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). MIT licensed.

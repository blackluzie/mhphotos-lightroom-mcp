# MHPhotos Lightroom MCP

Open-source Model Context Protocol integration for Adobe Lightroom Classic and extensible photography workflows.

> **Early prototype:** The server runs against a built-in **mock catalog** only. No Lightroom connection is implemented yet. Do not use it with a production catalog.

## Quick start (prototype only)

```bash
git clone https://github.com/blackluzie/mhphotos-lightroom-mcp.git
cd mhphotos-lightroom-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
mhphotos-lightroom-mcp
```

Tools (default backend is a **mock catalog**; nothing touches real photos until you opt in to the bridge):

| Tool | Purpose | Needs opt-in |
|------|---------|:-:|
| `connection_status` | Adapter mode, connection, enabled permissions | |
| `list_collections` | Collections (albums) with id, name, photo count; `name_contains` to find e.g. "09 2026" | |
| `list_photos` | Search photos (also by `location` text or GPS circle `near_lat`/`near_lon`/`radius_km`): `min_rating`, `year` or `captured_from`/`captured_to`, `keyword`, `flag`, optional collection; paginated, `limit` capped at 100 | |
| `get_photo_metadata` | Rating, flag, keywords, capture time, camera | |
| `set_rating` / `set_flag` / `add_keywords` | Edit metadata (dry run unless `confirm=true`) | `MHPHOTOS_ALLOW_WRITES=1` |
| `export_photos` | JPEG export to an operator-set folder (dry run unless `confirm=true`) | `MHPHOTOS_EXPORT_DIR=/abs/path` |
| `get_photo_previews` | Look at up to 12 photos at a time (images), with place and exposure | |
| `get_develop_settings` / `list_presets` | Current develop settings of a photo; your Lightroom presets | |
| `apply_preset` / `apply_develop_settings` / `create_preset` | Apply a preset or settings to photos, or create a new preset (dry run unless `confirm=true`; Lightroom keeps a history step) | `MHPHOTOS_ALLOW_WRITES=1` |
| `mhphotos_galleries` / `mhphotos_inbox` / `mhphotos_instagram_posts` | Read MHPhotos categories, Foto-Inbox with AI suggestions, your Instagram posts | `MHPHOTOS_INBOX_URL`, `MHPHOTOS_ADMIN_TOKEN` |
| `mhphotos_analyze_inbox` | Ask MHPhotos for AI title/description/category suggestions (changes nothing public) | same |
| `mhphotos_publish` | Publish inbox items on the **public** MHPhotos site (dry run unless `confirm=true`) | same + `MHPHOTOS_ALLOW_PUBLISH=1` |
| `send_to_mhphotos_inbox` | Export and send to the MHPhotos Foto-Inbox for review, nothing goes public (dry run unless `confirm=true`) | `MHPHOTOS_INBOX_URL`, `MHPHOTOS_ADMIN_TOKEN`, `MHPHOTOS_EXPORT_DIR` |

Example workflows (5-star photos of 2026 to mhphotos.de, Instagram posts): see [docs/workflows.md](docs/workflows.md).

## Use with real Lightroom Classic
1. Install the plugin in `plugin/mhphotos.lrdevplugin` (see [plugin/README.md](plugin/README.md)).
2. Run the server with `MHPHOTOS_ADAPTER=bridge`. In Claude Code the committed `.mcp.json` does this for you: run `pip install -e .` once in this repo, start Claude Code here and approve the project server. It enables reads only; edits, export and inbox stay off until you add the environment variables from the tool table.
3. Try it on a **disposable catalog first** and follow the checklist in `plugin/README.md`.

## Architecture

- `server/`: Python MCP server, adapters, bridge, permission gate
- `plugin/`: Lightroom Classic SDK (Lua) plugin + Lua tests
- `tests/`: automated tests (Python, plus a runner for the Lua tests)
- `docs/architecture.md`: protocol, security, permission model
- `docs/cloud-assessment.md`: Lightroom cloud API assessment

## Roadmap / status

1. ~~Local authenticated bridge and mock adapter~~ done
2. ~~Read-only commands for photos, collections and metadata~~ done (plugin untested inside Lightroom)
3. ~~Permission-gated edits and export~~ done (plugin untested inside Lightroom)
4. ~~Assess Lightroom cloud API~~ done: see `docs/cloud-assessment.md`, not built
5. ~~MHPhotos publishing~~ done: `send_to_mhphotos_inbox` feeds the MHPhotos Foto-Inbox (`apps-platform/apps/mhphotos`, `/api/admin/lightroom-import`); approval stays in the MHPhotos app

Lightroom Classic catalogs should be stored locally, even if originals live on mounted NAS volumes. The server never writes to the catalog SQLite database.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Contributions welcome.

## License

MIT. Independent community project; not affiliated with or endorsed by Adobe.

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
| `list_collections` | Collections with id, name, photo count | |
| `list_photos` | Photos (optionally per collection), paginated, `limit` capped at 100 | |
| `get_photo_metadata` | Rating, flag, keywords, capture time, camera | |
| `set_rating` / `set_flag` / `add_keywords` | Edit metadata (dry run unless `confirm=true`) | `MHPHOTOS_ALLOW_WRITES=1` |
| `export_photos` | JPEG export to an operator-set folder (dry run unless `confirm=true`) | `MHPHOTOS_EXPORT_DIR=/abs/path` |

## Use with real Lightroom Classic
1. Install the plugin in `plugin/mhphotos.lrdevplugin` (see [plugin/README.md](plugin/README.md)).
2. Run the server with `MHPHOTOS_ADAPTER=bridge`.
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
5. Optional MHPhotos publishing tools: not started (needs a definition)

Lightroom Classic catalogs should be stored locally, even if originals live on mounted NAS volumes. The server never writes to the catalog SQLite database.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Contributions welcome.

## License

MIT. Independent community project; not affiliated with or endorsed by Adobe.

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

Read-only MCP tools (backed by a mock adapter with sample data; they never access or change real photos):

| Tool | Purpose |
|------|---------|
| `connection_status` | Adapter mode and connection state (`mock`) |
| `list_collections` | Collections with id, name, photo count |
| `list_photos` | Photos (optionally per collection), paginated, `limit` capped at 100 |
| `get_photo_metadata` | Rating, flag, keywords, capture time, camera |

A real Lightroom bridge will plug in later as another `CatalogAdapter` (`server/mhphotos_lightroom_mcp/adapters.py`).

## Architecture

- `server/`: Python MCP server
- `plugin/`: planned Lightroom Classic SDK (Lua) plugin
- `adapters/`: planned optional adapters, including cloud only if Adobe APIs allow it
- `tests/`: automated tests
- `docs/architecture.md`: architecture and safety

## Roadmap

1. ~~Mock adapter with read-only tools~~ (done); design a local, authenticated bridge.
2. Add read-only Lightroom Classic SDK commands for photos, collections and metadata.
3. Add permission-gated edits and exports.
4. Assess Lightroom Cloud API access separately.
5. Add optional MHPhotos publishing tools.

Lightroom Classic catalogs should be stored locally, even if originals live on mounted NAS volumes. Never directly write to the catalog SQLite database.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Contributions welcome.

## License

MIT. Independent community project; not affiliated with or endorsed by Adobe.

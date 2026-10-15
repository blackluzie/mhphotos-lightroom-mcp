# Architecture

```text
MCP client (Claude / Codex / other)
          |  stdio
          v
Python MCP server  ──(permission gate: reads free, writes opt-in + confirm)
          |  CatalogAdapter
          +── MockAdapter            (default, sample data, used by CI)
          +── BridgeAdapter ──> Bridge (HTTP, 127.0.0.1 only, bearer token)
                                  ^
                                  |  long-poll: plugin asks for commands, posts results
                                  |
                 Lightroom Classic plugin (Lua, plugin/mhphotos.lrdevplugin)
                                  |
                       Lightroom Classic SDK  ->  catalog / mounted originals
```

A network-mounted original does not imply a network-hosted Classic catalog. The
catalog must stay on a local disk. The server never opens the catalog SQLite file.

## Why the plugin connects to the server (not the other way round)
Lightroom plugins can make HTTP requests but cannot listen on a socket. The MCP
server therefore runs a small loopback HTTP endpoint and the plugin **long-polls**
it: `POST /v1/poll` blocks up to ~10 s and returns either `{"command": null}` or one
command; the plugin executes it and answers with `POST /v1/result`.

## Bridge protocol (v1)
All requests: JSON bodies (max 1 MB), header `Authorization: Bearer <token>`.

| Request | Meaning |
|---------|---------|
| `GET /v1/health` | `{"ok": true}` (auth required) |
| `POST /v1/poll` | → `{"command": {"id", "name", "args"}}` or `{"command": null}` |
| `POST /v1/result` | `{"id", "ok": true, "data": ...}` or `{"id", "ok": false, "error": "..."}` |

Plugin errors starting with `not_found:` become "not found" results on the MCP side.
Commands: `list_collections`, `list_photos`, `get_photo`, `set_rating`, `set_flag`,
`add_keywords`, `export_photos`.

### Discovery and security
- The server binds **127.0.0.1 only** (non-loopback hosts are refused in code).
- On start it writes `~/.mhphotos-lightroom-mcp/bridge.json` (`url`, `token`, mode 0600).
  The plugin re-reads it on every cycle, so server restarts (new token) are picked up.
- Token compare is constant-time; unauthenticated requests get 401; oversize or
  non-JSON bodies get 400.
- Commands originate only from the MCP server. The plugin cannot inject commands.
- Port: `MHPHOTOS_BRIDGE_PORT` (default 47831). Token override: `MHPHOTOS_BRIDGE_TOKEN`.

### Behaviour when things are missing
- **Lightroom closed / plugin not polling for 30 s:** tools return
  `{"error": "...not connected...", "code": "unavailable"}`; `connection_status` shows
  `connected: false`.
- **Plugin does not answer in 30 s (600 s for export):** `code: "unavailable"` (timeout).
- **NAS disconnected:** originals are offline in Lightroom; metadata reads still work
  (they come from the catalog), exports of missing originals fail inside Lightroom and
  are reported as `code: "plugin_error"`.
- **Lightroom blocks catalog writes** (e.g. another plugin holds write access): edit
  returns `plugin_error` (`write_blocked`); nothing is changed.

## Permission model (Phase 3)
Reads are always allowed. Anything that changes data needs **two** things:
1. **Operator opt-in** via environment, set outside the MCP conversation:
   - `MHPHOTOS_ALLOW_WRITES=1` enables `set_rating`, `set_flag`, `add_keywords`.
   - `MHPHOTOS_EXPORT_DIR=/absolute/path` enables `export_photos`; the client cannot
     choose the destination.
2. **`confirm=true`** on the individual call. Without it the tool returns a dry-run
   preview (current values, intended change) and changes nothing.

Further limits: max 100 photos per call, duplicate/unknown ids rejected before any
change, keywords are only ever added (never removed), ratings 0-5, flags
`none|picked|rejected`. There is no delete, no develop-settings edit and no
catalog-structure change.

## MHPhotos publishing (Phase 5)
`send_to_mhphotos_inbox` exports the chosen photos into a fresh `inbox-<id>` subfolder of
`MHPHOTOS_EXPORT_DIR` and uploads each JPEG to the MHPhotos Foto-Inbox
(`POST {MHPHOTOS_INBOX_URL}/api/admin/lightroom-import`, header `X-Admin-Token`, fields
`file`, `rating`, `keywords`, `source=lightroom-classic-plugin`). Items arrive with status
`wartet`; reviewing and publishing happens only inside the MHPhotos app. The inbox
deduplicates by file hash and EXIF (reported as `duplicate`).

Safeguards: dry run unless `confirm=true`; the target URL and token come only from the
operator's environment (https required, plain http only for localhost); the token is never
returned in results; files over the inbox limit of 24 MB are skipped and reported. The
exported files stay in the export folder.

## MHPhotos app tools
Beyond the upload, the server talks to the MHPhotos admin API (same URL/token as the
upload): `GET /api/admin/galleries`, `GET /api/admin/lightroom-import` (inbox with AI
suggestions), `POST .../{id}/analyze` (queued AI analysis), `PATCH .../{id}`
`{action: "approve", ...}` (**this publishes to the public site**) and
`GET /api/admin/instagram-library`. `mhphotos_publish` is gated by
`MHPHOTOS_ALLOW_PUBLISH=1` plus `confirm=true`, shows a full preview first, skips items that
are already public, rejected, untitled or without a valid category, and never creates new
categories. See [workflows.md](workflows.md).

## Adapter layer
The server talks only to the `CatalogAdapter` protocol (`adapters.py`). `MockAdapter`
gives deterministic data for development and CI; `BridgeAdapter` forwards to the plugin.
Tools and tests are identical for both. Select with `MHPHOTOS_ADAPTER=mock|bridge`.

## Test strategy
- Python: unit tests per tool, permission-gate tests, bridge tests with a fake plugin that
  speaks the real HTTP protocol, and stdio end-to-end tests (mock and bridge mode).
- Lua (5.1, same as Lightroom): JSON module and command logic against a *simulated*
  Lightroom API. This does **not** prove the real SDK calls are right — that needs the
  manual checklist in `plugin/README.md` against a disposable catalog.

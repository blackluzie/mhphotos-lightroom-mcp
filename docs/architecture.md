# Architecture (draft)

```text
MCP client (Codex / other clients)
          |
          | stdio
          v
Python MCP server
          |
          | local authenticated bridge (planned)
          v
Lightroom Classic SDK plugin (Lua)
          |
          v
Lightroom Classic catalog / mounted originals
```

The cloud adapter is a separate future integration, contingent on Adobe's available APIs and permissions. A network-mounted original does not imply a network-hosted Classic catalog.

## Phase 1 acceptance criteria
- Local bridge only, authenticated and bounded.
- Read-only photo/collection/metadata operations via supported SDK APIs.
- No direct catalog database writes.
- Integration tests against a disposable catalog.
- Explicitly documented behavior when Lightroom is closed or the NAS is disconnected.

## Adapter layer (implemented)
The server talks only to a read-only `CatalogAdapter` protocol (`adapters.py`). `MockAdapter` provides fixed sample data for development and CI; the Lightroom bridge will be a second implementation, so tools and tests stay unchanged. Pagination is bounded (max 100 per page) and unknown ids return an `error` result instead of raising.

Open design point: Lightroom SDK plugins can act as network clients but not as servers, so the Lua plugin should connect to the Python server (not the other way around).

# Local Lightroom bridge protocol (draft)

This is a **design draft**, not a running Lightroom Classic integration.

## Security baseline

- Bind the eventual HTTP listener to `127.0.0.1` only.
- Require a random bearer token, provisioned locally; never commit tokens.
- Permit read-only routes only. No arbitrary Lua evaluation, SQL, filesystem paths,
  catalog modifications, or remote network exposure.
- Limit response sizes and use short request timeouts.
- Lightroom catalog access must go through the Lightroom Classic SDK from a
  Lightroom plugin. Never write to Lightroom's SQLite catalog directly.

## Proposed routes

- `GET /v1/status` — `{"connected":true,"catalog":"Test Catalog"}`
- Future: `GET /v1/photos?limit=50` — paginated, allowlisted metadata only.

## Next milestone

Implement and test a Lightroom Classic Lua plugin that exposes safe catalog
metadata through the local transport, using a dedicated test catalog. Confirm
SDK compatibility and permission boundaries on macOS before enabling live reads.

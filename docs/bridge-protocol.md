# Local read-only bridge protocol

All routes bind to IPv4 loopback and require the private bearer credential.

- `GET /v1/status`: bridge readiness and bounded snapshot diagnostics, with no live catalog claim.
- `GET /v1/capabilities`: protocol version 2, metadata allowlist and limits.
- `POST /v1/selection`: submit explicitly selected metadata; at most 50 photos, 64 KiB encoded body.
- `GET /v1/selection`: return the last submitted snapshot, or 404 if absent/expired.

The selection is in memory for five minutes. Unknown fields, invalid types,
unauthenticated requests and browser cross-origin submissions are rejected.
PUT/PATCH/DELETE and arbitrary catalog operations remain unavailable.

See [Mac read-only functions](mac-readonly-functions.md) for the complete
field projection, legacy compatibility and validation details.

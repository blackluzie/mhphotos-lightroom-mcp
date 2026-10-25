# Private Classic read/export connector

`python -m mhphotos_lightroom_mcp.http_service` reuses the existing single-owner
GitHub OAuth integration, HTTPS issuer, exact callback allowlist and persistent
client registration database. Application secrets remain outside the repository.
The consent page explicitly names catalog reads and confirmed local JPEG exports.
Access and refresh tokens remain ephemeral; restart requires reconnection.

The public tool allowlist is exactly: `connection_status`, `list_collections`,
`list_photos`, `get_photo_metadata`, `get_photo_previews`, `get_contact_sheet`,
`export_photos`. No edit, MHPhotos-site or Instagram tools are registered.
The service also clears all write/publish/Instagram environment opt-ins.

Export is disabled without an operator-configured absolute `MHPHOTOS_EXPORT_DIR`.
Clients cannot supply a destination; `confirm=false` returns a dry run. Confirmed
exports retain the existing human-approval gate and write only local JPEG files.
Export is annotated as mutating, non-destructive and non-idempotent. It must not
be advertised as a read-only tool. Catalog ratings and edits are not changed.

The process owns the existing loopback bridge at 127.0.0.1:47831 and the OAuth
HTTP service at 127.0.0.1:8766. It reuses the existing mode-0600 local bridge
configuration/token and does not rewrite it. A separate heartbeat bridge on the
same port must be stopped before starting this process. Lightroom Classic must
remain running, and originals must be accessible for an unconstrained JPEG export.
This is a local Classic integration, not autonomous Adobe Cloud access.

Existing desktop OAuth config: `~/.config/mhphotos-lightroom/desktop.json`.
GitHub client secret: `~/.config/mhphotos-lightroom/github-client-secret` (0600;
parent 0700). Current bridge config: `~/.mhphotos-lightroom-mcp/bridge.json` (0600).
Keep `MHPHOTOS_CONFIRMATION` on its default `ask`; never set catalog-write or
publication opt-ins for this connector. No Meta or Adobe Cloud credentials are
needed for this local read path.

Deployment on the current Mac uses the existing HTTP LaunchAgent, an explicit
shell entry point and ProcessType Interactive. Preserve Tailscale Funnel routing
to loopback 8766; never expose the token-authenticated bridge port itself.

The source JPEG export requests quality 0.9 with resizing disabled. It is a
rendered JPEG with Lightroom adjustments, not the original RAW file. Validate
the actual output file, dimensions and hash before downstream use. Missing
originals, unavailable NAS storage or cold/background Lightroom tasks can cause
timeouts; a heartbeat alone does not establish that an export works.

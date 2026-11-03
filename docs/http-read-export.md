# Private Classic read/export connector

`python -m mhphotos_lightroom_mcp.http_service` reuses the existing single-owner
GitHub OAuth integration, HTTPS issuer, exact provider-neutral callback allowlist and persistent
client registration database. Application secrets remain outside the repository.
The consent page explicitly names catalog reads and confirmed local JPEG exports.
Access and refresh tokens remain ephemeral; restart requires reconnection.

The public tool allowlist is: `connection_status`, `list_collections`, `list_photos`,
`get_photo_metadata`, `get_photo_previews`, `get_contact_sheet`, `get_develop_settings`,
`list_presets`, `list_changes`, `catalog_audit_start`, `catalog_audit_status`,
`catalog_audit_results`, `photos_by_date`, `suggest_locations` (reads; the audit runs on the Mac and changes
nothing) and `export_photos`. No MHPhotos-site, publishing or
Instagram tools are registered, and nothing deletes photos.

**MHPhotos reads (opt-in).** `rebuild-mac.sh --mhphotos-url https://<site>` stores `mhphotos_url`; the service
then also offers `mhphotos_instagram_posts`, `mhphotos_instagram_images` and `mhphotos_inbox` (read-only; the admin
token is read from the Keychain entry `mhphotos-admin-token`, never from the client). `export_photos` accepts
`instagram_post_id` for a single photo; it travels into the inbox and lets the app link the post when published.
With the site configured the service also offers `mhphotos_instagram_queue`, `mhphotos_best_times` and
`instagram_candidates` (reads).

**Instagram planning (opt-in).** `rebuild-mac.sh --mhphotos-url https://<site> --allow-instagram` stores
`"allow_instagram": true`; the service then offers `mhphotos_schedule_instagram` and `mhphotos_cancel_instagram`.
Planned posts are published by the MHPhotos social queue at the planned time without further questions, so every
plan is a dry run until `confirm=true`, and in claude.ai these two tools should stay on "ask". The consent page
names this right. `--no-allow-instagram` removes it again.

**Catalog edits (opt-in).** `rebuild-mac.sh --allow-writes --apply --start` stores
`"allow_writes": true` in `desktop.json`; the service then also offers `set_rating`, `set_flag`
(`"rejected"` = Lightroom's *Abgelehnt*), `set_color_label`, `set_title_caption`, `add_keywords`,
`create_collection`, `add_to_collection`, `create_virtual_copies`, `undo_last_change`,
`apply_preset`, `apply_develop_settings`, `sync_develop_settings`, `create_preset` and
`catalog_audit_collections` (review collections only; nothing is moved or removed) and `set_location`, and the
consent page names these rights. Every edit is a dry run until `confirm=true`. Because this
stateless service cannot ask the user itself, the human check is the client's per-tool
permission: in claude.ai set the write tools to "ask". Ratings, flags, labels, titles and
keywords can be undone with `undo_last_change`; develop changes are Lightroom history steps.
`--no-allow-writes` returns to read/export only (reconnect clients after the restart).

Export is disabled without an operator-configured absolute `MHPHOTOS_EXPORT_DIR`.
Clients cannot supply a destination; `confirm=false` returns a dry run. Confirmed
exports write only local JPEG files. This stateless HTTP service cannot ask the user
itself (MCP elicitation needs a server-to-client request, which never arrives here and
used to hang the call), so the human check is the client's tool permission: in claude.ai
set *export_photos* to "ask" (hand icon) under the connector's tool permissions.
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

Other OAuth MCP clients and native loopback callbacks: [mcp-clients.md](mcp-clients.md).
Automated installation/recovery: [rebuild.md](rebuild.md).

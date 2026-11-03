# Private completed JPEG delivery

A separate read-only HTTP process serves completed exports on loopback 8768. Tailscale Serve HTTPS 9443 exposes it only inside the tailnet; never enable Funnel for this port. Existing public MCP/Funnel 443 remains separate.

Start `python -m mhphotos_lightroom_mcp.asset_delivery` with `MHPHOTOS_EXPORT_DIR` pointing to the same private export root as the MCP HTTP service. Use a 32-byte random hex token in `~/.config/mhphotos-lightroom/automation-token`, owner-only mode 600. Store its matching Bearer authorization only in an encrypted n8n HTTP Header Auth credential. Never commit it.

Only `export-*/ready.json` batches created after successful export and JPEG validation are eligible. Incomplete batches, symlinks, path traversal, wrong sizes and wrong SHA256 hashes are excluded. API metadata omits local paths. OpenAPI: `asset-delivery.openapi.json`.

The manual n8n workflow lists eligible JPEGs, downloads each file over authenticated private HTTPS, computes SHA256, stops on mismatch and retains the verified binary. It has no schedules, catalog edits or publishing nodes. Both the Mac and its Tailscale connection must be available. Lightroom and source storage must be available for new exports. The delivery token grants access to all marked completed exports and should be rotated if exposed.

Restarting the MCP HTTP service currently invalidates its in-memory OAuth tokens; the Chat connection must then be reconnected. Restart the asset delivery process independently when possible.

## Into the MHPhotos inbox, started by each export

`examples/n8n-jpegs-to-inbox.json` extends the workflow above: it reads the MHPhotos inbox first, skips every
asset whose SHA-256 is already there (the app's own fingerprint), downloads and verifies the rest and uploads
them to `POST /api/admin/lightroom-import` (status `wartet`, nothing goes live). Uploads are spaced 6 seconds
apart because the app accepts 12 uploads per minute. Nothing is remembered in n8n: a failed upload is simply
retried on the next run, and the app rejects duplicates itself.

It needs two n8n Header Auth credentials, one per server:

| Credential | used by | Header name | Value |
|---|---|---|---|
| MHPhotos Lightroom – private Exporte | Dateiliste, JPEG laden | `Authorization` | `Bearer ` + `automation-token` (Mac) |
| MHPhotos – Admin-Token | Inbox laden, In Inbox hochladen | `X-Admin-Token` | MHPhotos admin token |

Replace `YOUR-MAC.YOUR-TAILNET.ts.net` and `YOUR-MHPHOTOS-DOMAIN` after import. If an export carried `instagram_post_id`, the asset
listing shows `instagram_id` and the upload sends it as `instagramId`, so the app links the Instagram post when the
photo is taken over.

**Automatic start.** The workflow also has a webhook trigger. When `n8n_webhook_url` is configured
(`rebuild-mac.sh --n8n-webhook-url https://YOUR-N8N-HOST:9443/webhook/mhphotos-export-ready --apply --start`),
every confirmed `export_photos` posts `{"event": "mhphotos.export_ready", "batch", "assets": [{id, filename, bytes}]}`
with header `X-MHPhotos-Token` to that URL. No local paths are sent. The token is generated once in
`~/.config/mhphotos-lightroom/n8n-webhook-token` (mode 600) and goes into a third n8n Header Auth credential
(*MHPhotos – Webhook vom Mac*, header name `X-MHPhotos-Token`). Activate the workflow so the production webhook URL
answers. A failed notification never fails the export; the tool result reports `n8n_notified: false` and a manual
run picks the batch up. Keep n8n private (Tailscale): the Mac reaches it over the tailnet, nothing is public.

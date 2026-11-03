# Private completed JPEG delivery

A separate read-only HTTP process serves completed exports on loopback 8768. Tailscale Serve HTTPS 9443 exposes it only inside the tailnet; never enable Funnel for this port. Existing public MCP/Funnel 443 remains separate.

Start `python -m mhphotos_lightroom_mcp.asset_delivery` with `MHPHOTOS_EXPORT_DIR` pointing to the same private export root as the MCP HTTP service. Use a 32-byte random hex token in `~/.config/mhphotos-lightroom/automation-token`, owner-only mode 600. Store its matching Bearer authorization only in an encrypted n8n HTTP Header Auth credential. Never commit it.

Only `export-*/ready.json` batches created after successful export and JPEG validation are eligible. Incomplete batches, symlinks, path traversal, wrong sizes and wrong SHA256 hashes are excluded. API metadata omits local paths. OpenAPI: `asset-delivery.openapi.json`.

The manual n8n workflow lists eligible JPEGs, downloads each file over authenticated private HTTPS, computes SHA256, stops on mismatch and retains the verified binary. It has no schedules, catalog edits or publishing nodes. Both the Mac and its Tailscale connection must be available. Lightroom and source storage must be available for new exports. The delivery token grants access to all marked completed exports and should be rotated if exposed.

Restarting the MCP HTTP service currently invalidates its in-memory OAuth tokens; the Chat connection must then be reconnected. Restart the asset delivery process independently when possible.

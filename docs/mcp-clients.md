# Other MCP clients

The endpoint is `https://YOUR-MAC.YOUR-TAILNET.ts.net/mcp` (your `--public-url`)
(Streamable HTTP). The Mac, Tailscale and Lightroom Classic must run; new exports
also require the originals/NAS. This is Lightroom Classic, not the Adobe Cloud API.

## Remote clients

The OAuth server accepts public clients with Dynamic Client Registration (DCR),
Authorization Code with PKCE S256, and scope `lightroom:read`. Discovery is available
at `/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource/mcp`. The resource/audience is the exact
HTTPS `/mcp` URL. Registration alone grants no access: GitHub login is limited to
the configured owner (`--github-owner-id`), followed by explicit consent. Tokens are bound
to their client and audience. Refresh tokens rotate and replay revokes their family.

Add each client's **exact** callback to `desktop.json` through the rebuild command:

```sh
./scripts/rebuild-mac.sh --callback https://claude.ai/api/mcp/auth_callback \
  --callback https://claude.com/api/mcp/auth_callback
# Review the plan, then repeat with --apply --start.
```

Claude documents these callback URLs at
<https://support.anthropic.com/en/articles/11503834-building-custom-connectors-via-remote-mcp-servers>.
In Claude's remote connector settings enter the MCP endpoint above, then authenticate
as the permitted GitHub owner and approve the displayed callback/rights. No shared
MCP password or static access token is required. A real Claude UI login remains a
separate client-specific acceptance test; protocol tests do not establish that UI.
Clients requiring CIMD-only registration or confidential client secrets are not
implemented. Do not claim universal compatibility.

Other clients may use HTTPS callbacks or exact native loopback callbacks such as
`http://127.0.0.1:45123/callback/client-id`. First determine the real callback from
that client; configure its fixed local port if supported, then add the exact URL.
No arbitrary ports, wildcards, `localhost`, credentials, query strings, fragments,
or cleartext remote HTTP callbacks are accepted. A changed ephemeral port requires
an explicit configuration update. Restart the managed HTTP service to activate
changes; current in-memory OAuth tokens are lost, so reconnect existing clients.
The legacy `MHPHOTOS_CHATGPT_REDIRECT_URIS` environment variable still works;
`MHPHOTOS_REDIRECT_URIS` is the provider-neutral name. The deployed wrapper reads
`~/.config/mhphotos-lightroom/desktop.json`.

## Local clients (stdio)

The repo's `.mcp.json` and `docs/setup-mac.md` describe local stdio use. On a separate
Mac/server instance set `MHPHOTOS_ADAPTER=bridge`, keep write/publish flags disabled,
and run the locked environment's `mhphotos-lightroom-mcp`. A local stdio server
owns its own bridge. **Do not start a second bridge process on port 47831 on the
current Mac** while the HTTP service owns it. Prefer the existing remote endpoint
for concurrent clients, or deliberately stop the HTTP service before local mode.

## Rights

The remote endpoint offers six reads plus gated local JPEG export. No rating,
editing, deletion or Instagram/site publication tools are exposed. All clients
share the same owner/catalog; this is not a multi-user hosted product. The private
JPEG delivery API on port 9443 is a separate HTTP API for n8n, not another MCP URL.

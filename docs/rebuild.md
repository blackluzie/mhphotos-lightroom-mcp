# Rebuild and recover the MHPhotos connection

The repo automates the Mac runtime, private configuration, LaunchAgents, optional
Tailscale routes, and plugin copy. It does not install macOS, Lightroom, Tailscale,
NAS credentials, or restore a Lightroom catalog. Keep the catalog/originals backed
up separately. Do not put photos, client secrets, keys or token databases in Git.

## Reproducible runtime and plan

Use a reviewed commit of this repository, macOS, Python 3.11+ and `uv`. The installer
uses `uv sync --frozen --no-dev` and committed `uv.lock`, including package hashes.
Do not follow a moving Git branch for disaster recovery: record the deployed SHA.

```sh
git clone https://github.com/blackluzie/mhphotos-lightroom-mcp.git
cd mhphotos-lightroom-mcp
git checkout <reviewed-commit>
./scripts/rebuild-mac.sh --public-url https://YOUR-MAC.YOUR-TAILNET.ts.net \
  --callback https://claude.ai/api/mcp/auth_callback \
  --github-secret-file /absolute/path/to/restored-github-client-secret
```

Without `--apply`, only the Python environment is installed/refreshed; no config,
plugin, service or routing changes are made. The GitHub secret source must be an
owner-controlled regular file with mode 600. Restore the existing GitHub OAuth app
secret; its registered app callback must match the issuer's `/auth/github/callback`.
The current private deployment is tied to Martin's GitHub owner identity and app.
Changing owner/app is a separate migration, not an installer parameter.

Repeat with `--apply` to write configuration and two LaunchAgents. Existing tokens,
GitHub secret, bridge token and registry remain intact. Existing callbacks are
retained. Changed config/agent files receive private `before-rebuild` copies.
The issuer cannot be silently changed. Private directories are hardened to 700;
files use 600. Existing plugins are preserved, never overwritten.

Add `--start` to load/restart **only** `de.mhphotos.lightroom.http` and
`de.mhphotos.lightroom.assets`. Add `--configure-routing` to inspect/create only
public MCP/Funnel 443 → loopback 8766 and private HTTPS 9443 → loopback 8768.
Conflicting handlers/listeners and a public asset port cause refusal. Other ports
are preserved. No `tailscale reset`, global service restart or Docker operation
is performed. Tailscale must already be installed, signed in and entitled to
Serve/Funnel. Granting those account permissions is a prerequisite, not automated.

On a new Mac add the copied `.lrdevplugin` once in Lightroom's Plug-in Manager,
open the correct catalog, mount the NAS and reconnect MCP clients. Lightroom's UI
registration and account logins cannot be recovered merely from source code.
Run one metadata/preview/export check before relying on the connection. A healthy
bridge does not prove that NAS-backed originals can be exported.

## Encrypted connection backup

```sh
.venv/bin/python -m mhphotos_lightroom_mcp.recovery backup \
  --file /absolute/path/to/mhphotos-mac-config.enc \
  --key-file /absolute/path/to/private-recovery.key
```

A missing key is generated with mode 600. The authenticated Fernet archive contains
only desktop configuration, GitHub secret, bridge token, private delivery token,
and a consistent SQLite snapshot of the OAuth client registry. Photos, catalog,
exports, service plists, passwords/2FA for n8n and the recovery key are excluded.
Copy the encrypted archive off the Mac. Store the recovery key **separately**, e.g.
in a password manager/offline backup. GitHub alone cannot recover these secrets.

On a clean account, first install the locked runtime, then restore and apply:

```sh
uv sync --frozen --no-dev
.venv/bin/python -m mhphotos_lightroom_mcp.recovery restore \
  --file /absolute/path/to/mhphotos-mac-config.enc \
  --key-file /absolute/path/to/private-recovery.key
./scripts/rebuild-mac.sh --apply --start --configure-routing
```

Restore authenticates the archive before writing, accepts only known regular
files and refuses to replace a different existing configuration. Repeating the
same restore is safe. On a replacement Mac with a different Tailscale hostname,
OAuth app callback/issuer and n8n's asset URL need an explicit migration; do not
assume the previous DNS identity moves automatically. Tokens remain ephemeral:
reconnect ChatGPT/Claude after HTTP restart even when the registry is restored.

## n8n / Hetzner

The server stack and encrypted OneDrive recovery are in the other repository:
<https://github.com/blackluzie/apps-platform/tree/feat/mhphotos-n8n-stack/infra/mhphotos-n8n>.
The server restore drill rebuilds an **isolated disposable** copy, disables its
workflows before server startup, verifies paired credential decryption and
readiness, and has no external network or published ports. It preserves real
passwords/2FA and does not attempt a login using the obsolete setup password.
It never cuts over production or overwrites Kickdesk. A production disaster
recovery cutover must verify host identity, DNS, free ports, backup/key pairing
and restored login before enabling private access; that switch is deliberate.

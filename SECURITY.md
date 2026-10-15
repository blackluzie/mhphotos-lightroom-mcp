# Security Policy

This project is an early prototype and is not production-ready.

Please do not post vulnerabilities, secrets, catalog files, or private photographs in public issues. Contact the maintainer privately through GitHub before disclosure.

Design principles:
- Local-only bridge by default.
- Read-only operations first.
- Explicit user confirmation for changes.
- No direct SQLite writes to Lightroom catalogs.
- No public network exposure or hard-coded credentials.

Implemented protections:
- Bridge binds 127.0.0.1 only, bearer token (constant-time compare), request size limits.
- Changes need operator opt-in, `confirm=true` and, where the client supports it, a direct yes/no from the user.
- Local audit log (owner-only file) and undo for metadata changes.
- Admin token only from environment, file or macOS Keychain; never logged or returned.
- Downloaded Instagram images: https to Instagram/Meta hosts only, size-capped, redirects re-checked.
- Develop settings are validated against a whitelist with Lightroom's ranges.

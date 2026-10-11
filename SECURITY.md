# Security Policy

This project is an early prototype and is not production-ready.

Please do not post vulnerabilities, secrets, catalog files, or private photographs in public issues. Contact the maintainer privately through GitHub before disclosure.

Design principles:
- Local-only bridge by default.
- Read-only operations first.
- Explicit user confirmation for changes.
- No direct SQLite writes to Lightroom catalogs.
- No public network exposure or hard-coded credentials.

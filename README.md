# MHPhotos Lightroom MCP

Open-source, read-only Model Context Protocol integration for Adobe Lightroom
Classic. The Lightroom plugin explicitly sends metadata for up to 50 selected
photos to an authenticated local bridge. No catalog files or original images are
transferred, and Lightroom ratings cannot be changed.

**Prototype:** macOS selection transfer works locally. The installer supports
macOS and Windows; Windows Lightroom SDK execution and service logon startup
still need manual release verification. `connection_status` does not claim a
live Lightroom catalog connection. Selection snapshots expire after five minutes. Extended capture metadata, larger selections and detailed snapshot status are described in [Mac read-only functions](docs/mac-readonly-functions.md).

## Desktop installation

See [secure macOS and Windows installation](docs/installation.md). Preview a plan
first, then explicitly install into a separate user directory. Credentials are
created automatically or safely reused; no Lightroom token entry is required.
Installation does not register or restart a service, replace the active plugin,
or change a tunnel. Activation and rollback are documented separately.

Python 3.11+ and a reviewed, complete source checkout are required. This is a
source installer, not a signed native application distribution.

## Development

```sh
git clone https://github.com/blackluzie/mhphotos-lightroom-mcp.git
cd mhphotos-lightroom-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check server tests
```

On Windows, use `py -3` to create the environment and run commands with
`.venv\Scripts\python.exe`. Core tests also run on Linux; the desktop installer
supports macOS and Windows.

## MCP tools

- `connection_status`: reports local bridge readiness, without claiming direct
  access to a Lightroom catalog.
- `selected_photo_metadata`: returns only the last explicitly submitted,
  unexpired metadata snapshot.

Both tools are read-only. There are no rating-change or catalog-write tools.
The local bridge binds to `127.0.0.1` and requires a private bearer credential.
Optional Streamable HTTP/OAuth configuration is described in
[ChatGPT OAuth](docs/chatgpt-oauth.md); the installer does not enable public access.

## Architecture

- `server/`: MCP server, local bridge, private file transport and installer.
- `lightroom/`: Lightroom Classic SDK plugin.
- `tests/`: automated security and integration tests.
- `docs/`: protocol, installation and integration documentation.

Never directly write to Lightroom's catalog SQLite database. Additional catalog
operations, exports and cloud adapters remain future work.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
MIT. Independent community project; not affiliated with or endorsed by Adobe.

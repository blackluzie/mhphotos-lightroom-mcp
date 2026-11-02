#!/bin/sh
# Run from a reviewed checkout/ref. No sudo, no remote shell installation.
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
command -v uv >/dev/null || { echo 'Install uv first (https://docs.astral.sh/uv/); then rerun.' >&2; exit 1; }
[ "$(uname -s)" = Darwin ] || { echo 'macOS is required.' >&2; exit 1; }
cd "$repo_dir"
uv sync --frozen --no-dev
exec "$repo_dir/.venv/bin/python" -m mhphotos_lightroom_mcp.rebuild "$@"

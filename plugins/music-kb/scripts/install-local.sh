#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install uv first: https://docs.astral.sh/uv/" >&2
  exit 1
fi

# Install commands for the local machine. This never installs a database.
uv tool install --editable --force "$ROOT"
echo "Installed music-kb and music-kb-mcp. Run: music-kb --help"

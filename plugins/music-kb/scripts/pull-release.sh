#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <rsync-source-release-dir> [target-dir]" >&2
  echo "Example: $0 'kb-sync@publisher:/srv/music-kb/releases/music-kb-2026w29/'" >&2
  exit 2
fi

SOURCE="$1"
TARGET_DIR="${2:-$HOME/.music-kb}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGING="$(mktemp -d "${TMPDIR:-/tmp}/music-kb-release.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT

# rsync writes only a temporary local staging directory. The CLI verifies the
# manifest before atomically switching ~/.music-kb/current.sqlite.
rsync -a --partial --checksum "${SOURCE%/}/" "$STAGING/"
uv run --project "$ROOT" music-kb snapshot install \
  --release-dir "$STAGING" --target-dir "$TARGET_DIR"

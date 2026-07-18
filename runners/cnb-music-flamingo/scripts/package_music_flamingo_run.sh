#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
source "$ROOT/scripts/source_env.sh"

run_dir="$(python "$ROOT/scripts/music_flamingo_run_context.py" print-dir)"
if [[ ! -d "$run_dir" ]]; then
  echo "Run directory does not exist: $run_dir" >&2
  exit 1
fi

artifact_dir="$ROOT/cnb-artifacts"
artifact_path="$artifact_dir/music-flamingo-run.tar.gz"
mkdir -p "$artifact_dir"
rm -f "$artifact_path"

tar -C "$(dirname "$run_dir")" -czf "$artifact_path" "$(basename "$run_dir")"
if command -v sha256sum >/dev/null 2>&1; then
  digest="$(sha256sum "$artifact_path" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  digest="$(shasum -a 256 "$artifact_path" | awk '{print $1}')"
else
  digest="unavailable"
fi
printf 'run_dir=%s\n' "$run_dir"
printf 'artifact=%s\n' "$artifact_path"
printf 'sha256=%s\n' "$digest"

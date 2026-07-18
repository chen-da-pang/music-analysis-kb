#!/usr/bin/env bash
# Hydrate exactly the selected quality-rerun inputs, then verify every object.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

source_manifest="${MUSIC_FLAMINGO_QUALITY_SOURCE_MANIFEST:-data/input/campaign-kugou-20260706/manifest.jsonl}"
input_root="${MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT:-data/input/campaign-kugou-20260706}"
selection_file="${MUSIC_FLAMINGO_QUALITY_SELECTION_FILE:?MUSIC_FLAMINGO_QUALITY_SELECTION_FILE is required}"
source_expected_count="${MUSIC_FLAMINGO_QUALITY_SOURCE_EXPECTED_COUNT:-927}"
expected_count="${MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT:?MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT is required}"
campaign_id="${MUSIC_FLAMINGO_CAMPAIGN_ID:?MUSIC_FLAMINGO_CAMPAIGN_ID is required}"
run_dir="$(python scripts/music_flamingo_run_context.py print-dir)"

python scripts/prepare_kugou_quality_rerun.py \
  --source-manifest "$source_manifest" \
  --input-root "$input_root" \
  --repo-root "$root" \
  --selection-file "$selection_file" \
  --run-dir "$run_dir" \
  --source-expected-count "$source_expected_count" \
  --expected-campaign-id "$campaign_id"

selected_count="$(python3 - "$run_dir/quality_rerun_plan.json" <<'PYTHON'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["selected_item_count"])
PYTHON
)"
[[ "$selected_count" == "$expected_count" ]] || {
  echo "Quality rerun selected ${selected_count} items, expected ${expected_count}." >&2
  exit 2
}

if ! command -v git-lfs >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends git-lfs
  rm -rf /var/lib/apt/lists/*
fi
git lfs version
include_pattern="$(paste -sd, "$run_dir/quality_rerun_lfs_include.txt")"
[[ -n "$include_pattern" ]] || { echo 'Quality rerun has no LFS include paths.' >&2; exit 2; }
git lfs pull --include="$include_pattern" --exclude=""

python scripts/music_flamingo_campaign.py validate \
  --manifest "$run_dir/quality_rerun_manifest.jsonl" \
  --audio-root "$input_root" \
  --expected-count "$expected_count" \
  --expected-campaign-id "$campaign_id"

printf 'quality_rerun_manifest=%s\n' "$run_dir/quality_rerun_manifest.jsonl"
printf 'quality_rerun_audio_root=%s\n' "$root/$input_root"

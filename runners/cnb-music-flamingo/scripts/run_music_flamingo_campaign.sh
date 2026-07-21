#!/usr/bin/env bash
# Run one shard of a generated disposable Music Flamingo campaign.
#
# All campaign-specific values are injected by the generated .cnb.yml.  This
# script deliberately contains no weekly date, item count, repository URL, or
# shard index, so the same exported runner can be used by every campaign.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

campaign_id="${MUSIC_FLAMINGO_CAMPAIGN_ID:?MUSIC_FLAMINGO_CAMPAIGN_ID is required}"
source_manifest="${MUSIC_FLAMINGO_CAMPAIGN_SOURCE_MANIFEST:?MUSIC_FLAMINGO_CAMPAIGN_SOURCE_MANIFEST is required}"
input_root="${MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT:?MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT is required}"
expected_count="${MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT:?MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT is required}"
shard_index="${MUSIC_FLAMINGO_CAMPAIGN_SHARD_INDEX:?MUSIC_FLAMINGO_CAMPAIGN_SHARD_INDEX is required}"
shard_count="${MUSIC_FLAMINGO_CAMPAIGN_SHARD_COUNT:?MUSIC_FLAMINGO_CAMPAIGN_SHARD_COUNT is required}"
runtime_image="${CNB_RUNTIME_IMAGE:?CNB_RUNTIME_IMAGE is required}"

digest="${runtime_image##*@sha256:}"
if [[ "$digest" == "$runtime_image" || "${#digest}" -ne 64 ]] || ! printf '%s' "$digest" | grep -Eq '^[0-9a-f]{64}$'; then
  echo 'Campaign runtime must be pinned as image@sha256:<64 lowercase hex>.' >&2
  exit 2
fi
if [[ ! "$campaign_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "Unsafe campaign id: $campaign_id" >&2
  exit 2
fi
case "$shard_index" in ''|*[!0-9]*) echo 'Campaign shard index must be an integer.' >&2; exit 2 ;; esac
case "$shard_count" in ''|*[!0-9]*) echo 'Campaign shard count must be an integer.' >&2; exit 2 ;; esac
if [[ "$shard_index" -lt 1 || "$shard_count" -lt 1 || "$shard_index" -gt "$shard_count" ]]; then
  echo 'Campaign shard index/count is outside the allowed range.' >&2
  exit 2
fi
expected_shard_id="${campaign_id}-s${shard_index}"
if [[ "${MUSIC_FLAMINGO_CAMPAIGN_SHARD_ID:?MUSIC_FLAMINGO_CAMPAIGN_SHARD_ID is required}" != "$expected_shard_id" ]]; then
  echo "Campaign shard id must be $expected_shard_id." >&2
  exit 2
fi
if [[ ! -f "$source_manifest" ]]; then
  echo "Campaign source manifest is missing: $source_manifest" >&2
  exit 2
fi
expected_manifest_sha="${MUSIC_FLAMINGO_CAMPAIGN_MANIFEST_SHA256:-}"
if [[ -n "$expected_manifest_sha" ]]; then
  if [[ "${#expected_manifest_sha}" -ne 64 || "$expected_manifest_sha" == *[!0-9a-f]* ]]; then
    echo 'MUSIC_FLAMINGO_CAMPAIGN_MANIFEST_SHA256 must be 64 lowercase hex characters.' >&2
    exit 2
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    actual_manifest_sha="$(sha256sum "$source_manifest" | awk '{print $1}')"
  else
    actual_manifest_sha="$(shasum -a 256 "$source_manifest" | awk '{print $1}')"
  fi
  if [[ "$actual_manifest_sha" != "$expected_manifest_sha" ]]; then
    echo "Campaign manifest SHA-256 mismatch: $actual_manifest_sha != $expected_manifest_sha" >&2
    exit 2
  fi
fi

run_dir="$(python scripts/music_flamingo_run_context.py print-dir)"
mkdir -p "$run_dir"

# A newly created disposable repository has no ledger branch yet.  The restore
# helper falls back to the main branch and creates an empty local ledger; the
# first checkpoint then creates the dedicated result branch atomically.
bash scripts/campaign_ledger_git.sh restore "$run_dir/campaign_ledger.jsonl"
bash scripts/prepare_kugou_campaign_shard.sh

pending_count="$(python3 - "$run_dir/campaign_shard_plan.json" <<'PYTHON'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["pending_item_count"])
PYTHON
)"
printf '%s\n' "campaign_shard_pending_count=${pending_count}"

preflight_only="${MUSIC_FLAMINGO_CAMPAIGN_PREFLIGHT_ONLY:-0}"
case "$preflight_only" in
  0|false|False|FALSE|no|No|NO) preflight_only=0 ;;
  1|true|True|TRUE|yes|Yes|YES) preflight_only=1 ;;
  *) echo 'MUSIC_FLAMINGO_CAMPAIGN_PREFLIGHT_ONLY must be boolean.' >&2; exit 2 ;;
esac
if [[ "$preflight_only" == 0 && "${MUSIC_FLAMINGO_CAMPAIGN_MAX_PENDING_ITEMS:-0}" != 0 ]]; then
  echo 'MUSIC_FLAMINGO_CAMPAIGN_MAX_PENDING_ITEMS is reserved for explicit preflight.' >&2
  exit 2
fi

rc=0
if [[ "$preflight_only" == 1 ]]; then
  if [[ "${MUSIC_FLAMINGO_CAMPAIGN_MAX_PENDING_ITEMS:-0}" != 1 || "$pending_count" -gt 1 ]]; then
    echo 'Campaign preflight must select at most one pending item and set max_pending_items=1.' >&2
    exit 2
  fi
  bash scripts/campaign_ledger_git.sh checkpoint "$run_dir/campaign_ledger.jsonl"
  RUN_DIR="$run_dir" python3 - <<'PYTHON'
import json
import os
from pathlib import Path
run_dir = Path(os.environ["RUN_DIR"])
plan = json.loads((run_dir / "campaign_shard_plan.json").read_text(encoding="utf-8"))
report = {"status": "success", "campaign_status": "preflight_success", "campaign": plan}
for name in ("batch_report.json", "campaign_report.json"):
    (run_dir / name).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
elif [[ "$pending_count" -gt 0 ]]; then
  export MUSIC_FLAMINGO_INPUT_MANIFEST="$run_dir/campaign_shard_manifest.jsonl"
  export MUSIC_FLAMINGO_INPUT_AUDIO_ROOT="$PWD/${input_root}"
  export MUSIC_FLAMINGO_EXPECTED_ITEM_COUNT="$pending_count"
  python scripts/run_music_flamingo_batch.py || rc=$?
else
  RUN_DIR="$run_dir" python3 - <<'PYTHON'
import json
import os
from pathlib import Path
run_dir = Path(os.environ["RUN_DIR"])
plan = json.loads((run_dir / "campaign_shard_plan.json").read_text(encoding="utf-8"))
report = {"status": "success", "campaign_status": "already_complete_for_selected_shard", "campaign": plan}
for name in ("batch_report.json", "campaign_report.json"):
    (run_dir / name).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PYTHON
fi

printf '%s\n' "$rc" > "$run_dir/campaign_runner_exit_code.txt"
cat "$run_dir/campaign_shard_plan.json" || true
cat "$run_dir/campaign_report.json" || true
tail -n 100 "$run_dir/campaign_ledger.jsonl" || true

# Keep the same evidence boundary as the established runner routes.  The
# disposable repository is deleted only after the publisher has recovered the
# durable ledger and passed the local release/peer gates.
bash scripts/package_music_flamingo_run.sh
exit "$rc"

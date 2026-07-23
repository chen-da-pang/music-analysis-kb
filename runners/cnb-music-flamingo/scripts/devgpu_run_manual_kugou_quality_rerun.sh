#!/usr/bin/env bash
# Run an explicitly selected, isolated KuGou quality rerun from a Dev GPU
# terminal.  This is intentionally separate from the primary Dev GPU campaign
# wrapper: it must never derive a shard from the primary campaign ledger.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

[[ "${MUSIC_FLAMINGO_MANUAL_QUALITY_ROUTE:-}" == "1" ]] || {
  echo 'MUSIC_FLAMINGO_MANUAL_QUALITY_ROUTE=1 is required.' >&2
  exit 2
}

campaign_id="${MUSIC_FLAMINGO_CAMPAIGN_ID:?MUSIC_FLAMINGO_CAMPAIGN_ID is required}"
source_manifest="${MUSIC_FLAMINGO_QUALITY_SOURCE_MANIFEST:?MUSIC_FLAMINGO_QUALITY_SOURCE_MANIFEST is required}"
input_root="${MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT:?MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT is required}"
selection_file="${MUSIC_FLAMINGO_QUALITY_SELECTION_FILE:?MUSIC_FLAMINGO_QUALITY_SELECTION_FILE is required}"
source_manifest_sha256="${MUSIC_FLAMINGO_CAMPAIGN_MANIFEST_SHA256:?MUSIC_FLAMINGO_CAMPAIGN_MANIFEST_SHA256 is required}"
source_expected_count="${MUSIC_FLAMINGO_QUALITY_SOURCE_EXPECTED_COUNT:?MUSIC_FLAMINGO_QUALITY_SOURCE_EXPECTED_COUNT is required}"
expected_count="${MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT:?MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT is required}"
ledger_branch="${MUSIC_FLAMINGO_LEDGER_BRANCH:?MUSIC_FLAMINGO_LEDGER_BRANCH is required}"
execution_profile="${MUSIC_FLAMINGO_EXECUTION_PROFILE:?MUSIC_FLAMINGO_EXECUTION_PROFILE is required}"
expected_gpu="${MUSIC_FLAMINGO_MANUAL_GPU_NAME:-L40}"
max_selected_count="${MUSIC_FLAMINGO_MANUAL_MAX_SELECTED_COUNT:-5}"
max_utilization_percent="${MUSIC_FLAMINGO_MANUAL_GPU_MAX_UTILIZATION_PERCENT:-0}"
repetition_penalty="${MUSIC_FLAMINGO_REPETITION_PENALTY:?MUSIC_FLAMINGO_REPETITION_PENALTY is required}"
no_repeat_ngram_size="${MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE:?MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE is required}"

[[ "$repetition_penalty" == "1.08" ]] || {
  echo 'Manual quality route requires MUSIC_FLAMINGO_REPETITION_PENALTY=1.08.' >&2
  exit 2
}
[[ "$no_repeat_ngram_size" == "4" ]] || {
  echo 'Manual quality route requires MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE=4.' >&2
  exit 2
}

case "$expected_gpu" in
  L40) default_minimum_free_mib=40000 ;;
  H20) default_minimum_free_mib=87000 ;;
  *) default_minimum_free_mib=1 ;;
esac
minimum_free_mib="${MUSIC_FLAMINGO_MANUAL_GPU_MIN_FREE_MIB:-$default_minimum_free_mib}"

[[ "${MUSIC_FLAMINGO_DURABLE_LEDGER_REQUIRED:?MUSIC_FLAMINGO_DURABLE_LEDGER_REQUIRED is required}" == "1" ]] || {
  echo 'Manual quality route requires MUSIC_FLAMINGO_DURABLE_LEDGER_REQUIRED=1.' >&2
  exit 2
}
[[ "${MUSIC_FLAMINGO_LEDGER_CHECKPOINT_EVERY:?MUSIC_FLAMINGO_LEDGER_CHECKPOINT_EVERY is required}" == "1" ]] || {
  echo 'Manual quality route requires MUSIC_FLAMINGO_LEDGER_CHECKPOINT_EVERY=1.' >&2
  exit 2
}

run_dir="$(python scripts/music_flamingo_run_context.py print-dir)"
mkdir -p "$run_dir"

write_status() {
  local state="$1"
  local exit_code="${2:-}"
  if [[ -n "$exit_code" ]]; then
    python scripts/music_flamingo_run_context.py write-status \
      --state "$state" \
      --command-fragment devgpu_run_manual_kugou_quality_rerun.sh \
      --exit-code "$exit_code"
  else
    python scripts/music_flamingo_run_context.py write-status \
      --state "$state" \
      --command-fragment devgpu_run_manual_kugou_quality_rerun.sh
  fi
}

finish() {
  local rc="$1"
  trap - EXIT INT TERM
  printf '%s\n' "$rc" > "$run_dir/campaign_runner_exit_code.txt"
  if [[ "$rc" == 0 ]]; then
    write_status success "$rc"
  else
    write_status failed "$rc"
  fi
  exit "$rc"
}

trap 'rc=$?; finish "$rc"' EXIT
trap 'exit 143' INT TERM
write_status running

# This is a pure input/branch guard.  It reads the compact selection but never
# reads or derives pending items from the primary campaign ledger.
python scripts/manual_kugou_quality_route.py \
  --repo-root "$root" \
  --source-manifest "$source_manifest" \
  --input-root "$input_root" \
  --selection-file "$selection_file" \
  --source-manifest-sha256 "$source_manifest_sha256" \
  --source-expected-count "$source_expected_count" \
  --expected-count "$expected_count" \
  --campaign-id "$campaign_id" \
  --ledger-branch "$ledger_branch" \
  --expected-gpu "$expected_gpu" \
  --execution-profile "$execution_profile" \
  --minimum-free-mib "$minimum_free_mib" \
  --max-selected-count "$max_selected_count" \
  --max-utilization-percent "$max_utilization_percent" \
  --repetition-penalty "$repetition_penalty" \
  --no-repeat-ngram-size "$no_repeat_ngram_size" \
  --receipt "$run_dir/manual_quality_request.json"

# Gate 1 is deliberately before any model import.  A second independent gate
# runs after sparse LFS hydration, immediately before the model can load.
python scripts/check_manual_gpu_gate.py \
  --phase before_sparse_lfs_hydrate \
  --expected-gpu "$expected_gpu" \
  --minimum-free-mib "$minimum_free_mib" \
  --max-utilization-percent "$max_utilization_percent" \
  --receipt "$run_dir/manual_gpu_gate_before_hydrate.json"

bash scripts/campaign_ledger_git.sh restore "$run_dir/campaign_ledger.jsonl"
bash scripts/prepare_kugou_quality_rerun.sh

python scripts/check_manual_gpu_gate.py \
  --phase after_sparse_lfs_before_model \
  --expected-gpu "$expected_gpu" \
  --minimum-free-mib "$minimum_free_mib" \
  --max-utilization-percent "$max_utilization_percent" \
  --receipt "$run_dir/manual_gpu_gate_pre_model.json"

export MUSIC_FLAMINGO_INPUT_MANIFEST="$run_dir/quality_rerun_manifest.jsonl"
export MUSIC_FLAMINGO_INPUT_AUDIO_ROOT="$(cd "$input_root" && pwd -P)"
export MUSIC_FLAMINGO_EXPECTED_ITEM_COUNT="$expected_count"

python scripts/run_music_flamingo_batch.py

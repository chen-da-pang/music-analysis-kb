#!/usr/bin/env bash
# Prepare one deterministic KuGou shard, using LFS or ordinary Git blobs.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

source_manifest="${MUSIC_FLAMINGO_CAMPAIGN_SOURCE_MANIFEST:-data/input/campaign-kugou-20260706/manifest.jsonl}"
input_root="${MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT:-data/input/campaign-kugou-20260706}"
campaign_id="${MUSIC_FLAMINGO_CAMPAIGN_ID:?MUSIC_FLAMINGO_CAMPAIGN_ID is required}"
expected_count="${MUSIC_FLAMINGO_CAMPAIGN_EXPECTED_COUNT:-927}"
shard_index="${MUSIC_FLAMINGO_CAMPAIGN_SHARD_INDEX:?MUSIC_FLAMINGO_CAMPAIGN_SHARD_INDEX is required}"
shard_count="${MUSIC_FLAMINGO_CAMPAIGN_SHARD_COUNT:?MUSIC_FLAMINGO_CAMPAIGN_SHARD_COUNT is required}"
runtime_image="${CNB_RUNTIME_IMAGE:?CNB_RUNTIME_IMAGE is required}"
execution_profile="${MUSIC_FLAMINGO_EXECUTION_PROFILE:?MUSIC_FLAMINGO_EXECUTION_PROFILE is required}"
transport="${MUSIC_FLAMINGO_CAMPAIGN_TRANSPORT:-lfs}"
run_dir="$(python scripts/music_flamingo_run_context.py print-dir)"
ledger_path="$run_dir/campaign_ledger.jsonl"
prompt_path="$run_dir/campaign_prompt.txt"
plan_path="$run_dir/campaign_shard_plan.json"

mkdir -p "$run_dir"
printf '%s' "${MUSIC_FLAMINGO_PROMPT:?MUSIC_FLAMINGO_PROMPT is required}" > "$prompt_path"

python scripts/prepare_kugou_campaign_shard.py \
  --source-manifest "$source_manifest" \
  --input-root "$input_root" \
  --repo-root "$root" \
  --ledger "$ledger_path" \
  --run-dir "$run_dir" \
  --expected-count "$expected_count" \
  --campaign-id "$campaign_id" \
  --shard-index "$shard_index" \
  --shard-count "$shard_count" \
  --max-pending-items "${MUSIC_FLAMINGO_CAMPAIGN_MAX_PENDING_ITEMS:-0}" \
  --runtime-image "$runtime_image" \
  --prompt-file "$prompt_path" \
  --max-new-tokens "${MUSIC_FLAMINGO_MAX_NEW_TOKENS:-2048}" \
  --audio-clip-seconds "${MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS:-240}" \
  --model-id "${MUSIC_FLAMINGO_MODEL:-nvidia/music-flamingo-think-2601-hf}" \
  --model-revision "${MUSIC_FLAMINGO_REVISION:-1ea2109}" \
  --model-dir "${MUSIC_FLAMINGO_MODEL_DIR:-/opt/models/music-flamingo-think-2601-hf}" \
  --execution-profile "$execution_profile"

pending_count="$(python3 - "$plan_path" <<'PYTHON'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["pending_item_count"])
PYTHON
)"
printf '%s\n' "campaign_shard_pending_count=${pending_count}"

if [[ "$pending_count" == "0" ]]; then
  printf '%s\n' 'No pending items in this static shard; audio preparation skipped.'
  exit 0
fi

if [[ "$transport" == "git-objects" ]]; then
  # Ordinary Git storage is a no-cost fallback only for a bounded weekly
  # campaign.  The manifest is authoritative and carries every source size,
  # so fail before the GPU stage if a future campaign would make this route
  # inappropriate.
  python3 - "$source_manifest" \
    "${MUSIC_FLAMINGO_CAMPAIGN_GIT_OBJECTS_MAX_BYTES:-5000000000}" \
    "${MUSIC_FLAMINGO_CAMPAIGN_GIT_OBJECTS_MAX_FILE_BYTES:-268435456}" <<'PYTHON'
import json
import sys
from pathlib import Path

manifest, raw_total_cap, raw_file_cap = sys.argv[1:]
try:
    total_cap = int(raw_total_cap)
    file_cap = int(raw_file_cap)
except ValueError as exc:
    raise SystemExit("Git-object campaign size caps must be integers") from exc
if total_cap <= 0 or file_cap <= 0:
    raise SystemExit("Git-object campaign size caps must be positive")
rows = [json.loads(line) for line in Path(manifest).read_text(encoding="utf-8").splitlines() if line.strip()]
if not rows:
    raise SystemExit("Git-object campaign manifest is empty")
sizes = []
for index, row in enumerate(rows, start=1):
    size = row.get("source_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise SystemExit(f"Git-object manifest row {index} has invalid source_bytes: {size!r}")
    if size > file_cap:
        raise SystemExit(f"Git-object manifest row {index} exceeds per-file cap: {size} > {file_cap}")
    sizes.append(size)
total = sum(sizes)
if total > total_cap:
    raise SystemExit(f"Git-object campaign exceeds total cap: {total} > {total_cap}")
print(f"git_object_campaign_bytes={total}")
print(f"git_object_campaign_max_file_bytes={max(sizes)}")
PYTHON
fi

case "$transport" in
  lfs)
    if ! command -v git-lfs >/dev/null 2>&1; then
      # The promoted model image intentionally avoids toolchain churn.  CNB's
      # sparse campaign route needs only this small client package at runtime.
      apt-get update
      apt-get install -y --no-install-recommends git-lfs
      rm -rf /var/lib/apt/lists/*
    fi
    git lfs version
    # CNB's checkout already owns a pre-push hook.  The LFS local-install command
    # treats that hook as a conflict and exits 2; a sparse `git lfs pull` only
    # needs the client binary plus the repository's existing LFS endpoint config.
    # Do not overwrite the platform hook merely to download objects.
    include_path="$run_dir/lfs_include.txt"
    include_pattern="$(paste -sd, "$include_path")"
    if [[ -z "$include_pattern" ]]; then
      echo 'Shard plan has pending items but no LFS include patterns.' >&2
      exit 2
    fi
    git lfs pull --include="$include_pattern" --exclude=""
    ;;
  git-objects)
    printf '%s\n' 'Campaign transport is ordinary Git objects; LFS hydration skipped.'
    ;;
  *)
    echo "MUSIC_FLAMINGO_CAMPAIGN_TRANSPORT must be lfs or git-objects: $transport" >&2
    exit 2
    ;;
esac

python scripts/music_flamingo_campaign.py validate \
  --manifest "$run_dir/campaign_shard_manifest.jsonl" \
  --audio-root "$input_root" \
  --expected-count "$pending_count" \
  --expected-campaign-id "$campaign_id"

printf '%s\n' "campaign_shard_manifest=$run_dir/campaign_shard_manifest.jsonl"
printf '%s\n' "campaign_shard_audio_root=$root/$input_root"

#!/usr/bin/env bash
# Run one fully validated Git-object KuGou campaign shard in a CNB Dev GPU
# workspace.  This is deliberately foregrounded: CNB preserves the workspace
# while the stage runs and exposes its live stage log, while a process started
# after an arbitrary shell disconnect is not a trustworthy durability boundary.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

run_dir="$(python scripts/music_flamingo_run_context.py print-dir)"
mkdir -p "$run_dir"

write_status() {
  local state="$1"
  local exit_code="${2:-}"
  if [[ -n "$exit_code" ]]; then
    python scripts/music_flamingo_run_context.py write-status \
      --state "$state" \
      --command-fragment devgpu_run_kugou_campaign.sh \
      --exit-code "$exit_code"
  else
    python scripts/music_flamingo_run_context.py write-status \
      --state "$state" \
      --command-fragment devgpu_run_kugou_campaign.sh
  fi
}

finish() {
  local rc="$1"
  printf '%s\n' "$rc" > "$run_dir/campaign_runner_exit_code.txt"
  if [[ "$rc" == 0 ]]; then
    write_status success "$rc"
  else
    write_status failed "$rc"
  fi
  exit "$rc"
}

write_status running

# These checks run before model loading.  They prove that the fixed image has
# the requested L40 and that the selected shard's ordinary Git audio is intact.
bash scripts/check_remote_env.sh
bash scripts/campaign_ledger_git.sh restore "$run_dir/campaign_ledger.jsonl"
bash scripts/prepare_kugou_campaign_shard.sh

pending_count="$(python3 - "$run_dir/campaign_shard_plan.json" <<'PYTHON'
import json
import sys

print(json.load(open(sys.argv[1], encoding="utf-8"))["pending_item_count"])
PYTHON
)"

rc=0
if [[ "$pending_count" == 0 ]]; then
  # An empty pending manifest is not valid campaign input: the batch runner
  # deliberately rejects an expected item count of zero.  Complete this
  # idempotent shard without entering the runner or loading the model.
  RUN_DIR="$run_dir" python3 - <<'PYTHON'
import json
import os
from pathlib import Path

run_dir = Path(os.environ["RUN_DIR"])
plan = json.loads((run_dir / "campaign_shard_plan.json").read_text(encoding="utf-8"))
report = {
    "status": "success",
    "campaign_status": "already_complete_for_selected_shard",
    "campaign": plan,
}
for name in ("batch_report.json", "campaign_report.json"):
    (run_dir / name).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PYTHON
else
  export MUSIC_FLAMINGO_INPUT_MANIFEST="$run_dir/campaign_shard_manifest.jsonl"
  export MUSIC_FLAMINGO_INPUT_AUDIO_ROOT="$PWD/${MUSIC_FLAMINGO_CAMPAIGN_INPUT_ROOT}"
  export MUSIC_FLAMINGO_EXPECTED_ITEM_COUNT="$pending_count"

  set +e
  python scripts/run_music_flamingo_batch.py
  rc=$?
  set -e
fi

cat "$run_dir/campaign_shard_plan.json" || true
cat "$run_dir/campaign_report.json" || true
tail -n 100 "$run_dir/progress.jsonl" || true
finish "$rc"

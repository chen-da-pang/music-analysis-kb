#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export WORK_DIR="${WORK_DIR:-data/output/music_flamingo_pipeline}"
export MUSIC_FLAMINGO_OUTPUT_NAME="${MUSIC_FLAMINGO_OUTPUT_NAME:-devgpu_batch}"
export MUSIC_FLAMINGO_RUN_ID="${MUSIC_FLAMINGO_RUN_ID:-${CNB_BUILD_ID:-${CNB_PIPELINE_ID:-local}}}"

run_dir="$(python scripts/music_flamingo_run_context.py print-dir)"
mkdir -p "$run_dir"

cat > "$run_dir/README.txt" <<EOF2
Dev GPU workspace is ready.

Start or watch the batch from WebIDE terminal:
  bash scripts/devgpu_run_batch.sh

Run identity:
  ${MUSIC_FLAMINGO_RUN_ID}

Live files:
  ${run_dir}/run_status.json
  ${run_dir}/run.log
  ${run_dir}/progress.jsonl
  ${run_dir}/batch_report.json

This log server is only a viewer. It does not start the batch automatically.
EOF2

exec python scripts/devgpu_log_server.py

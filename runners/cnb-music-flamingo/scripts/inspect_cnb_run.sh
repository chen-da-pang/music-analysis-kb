#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/inspect_cnb_run.sh <SN> [PIPELINE_ID]

Environment:
  CNB_REPO=wuyoumusic/moss-music-runner
  CNB_INSPECT_TAIL_LINES=100

This is a read-only local inspector for a completed, failed, or cancelled CNB
run. It summarizes pipeline stages, prints the prepare-stage bottleneck lines,
and decodes the runner log when CNB exposes it.
EOF
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

duration_human() {
  local ms="${1:-0}"
  ms="${ms%.*}"
  if [[ ! "$ms" =~ ^[0-9]+$ ]]; then
    ms=0
  fi
  local total_seconds=$((ms / 1000))
  local hours=$((total_seconds / 3600))
  local minutes=$(((total_seconds % 3600) / 60))
  local seconds=$((total_seconds % 60))
  if ((hours > 0)); then
    printf "%dh%02dm%02ds" "$hours" "$minutes" "$seconds"
  elif ((minutes > 0)); then
    printf "%dm%02ds" "$minutes" "$seconds"
  else
    printf "%ds" "$seconds"
  fi
}

decode_base64_field() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import base64, sys; sys.stdout.write(base64.b64decode(sys.stdin.read().strip()).decode("utf-8", "replace"))'
    return
  fi
  if base64 --decode </dev/null >/dev/null 2>&1; then
    base64 --decode
    return
  fi
  if base64 -d </dev/null >/dev/null 2>&1; then
    base64 -d
    return
  fi
  base64 -D
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

sn="${1:-}"
pipeline_id="${2:-}"
repo="${CNB_REPO:-wuyoumusic/moss-music-runner}"
tail_lines="${CNB_INSPECT_TAIL_LINES:-100}"

if [[ -z "$sn" ]]; then
  usage >&2
  exit 2
fi

if [[ ! "$tail_lines" =~ ^[0-9]+$ || "$tail_lines" -lt 1 ]]; then
  echo "CNB_INSPECT_TAIL_LINES must be a positive integer." >&2
  exit 2
fi

require_command cnb
require_command jq
require_command python3

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

echo "== CNB run =="
echo "repo=$repo"
echo "sn=$sn"
echo

status_json="$(cnb build get-build-status --repo "$repo" --sn "$sn" --verbose)"
selector_args=()
if [[ -n "$pipeline_id" ]]; then
  selector_args=(--pipeline-id "$pipeline_id")
fi
if ! pipeline_id="$(printf '%s' "$status_json" | python3 scripts/cnb_pipeline_selector.py "${selector_args[@]}")"; then
  echo "Could not select pipeline for SN: $sn" >&2
  exit 1
fi

echo "pipeline_id=$pipeline_id"
jq -r '
  "run_status=" + (.data.status // "unknown"),
  "pipeline_status=" + (.data.pipelinesStatus[$pid].status // "unknown"),
  "pipeline_duration=" + ((.data.pipelinesStatus[$pid].duration // 0) | tostring) + "ms",
  "metric_core_hours=" + ((.data.pipelinesStatus[$pid].metricCoreHours // "") | tostring),
  "metric_duration=" + ((.data.pipelinesStatus[$pid].metricDuration // "") | tostring) + "ms"
' --arg pid "$pipeline_id" <<<"$status_json"
echo

echo "== Pipeline stages =="
jq -r '
  (.data.pipelinesStatus[$pid].stages // [])
  | .[]
  | "- " + (.id // "") + " | " + (.name // "") + " | status=" + (.status // "unknown") + " | duration=" + ((.duration // 0) | tostring) + "ms"
' --arg pid "$pipeline_id" <<<"$status_json"
echo

stage_json="$(cnb build get-build-stage --repo "$repo" --sn "$sn" --pipelineId "$pipeline_id" --stageId prepare --verbose)"
prepare_log="$tmpdir/prepare.log"
jq -r '(.data.content // [])[]?' <<<"$stage_json" >"$prepare_log"

echo "== Prepare summary =="
prepare_status="$(jq -r '.data.status // "unknown"' <<<"$stage_json")"
prepare_duration_ms="$(jq -r '.data.duration // 0' <<<"$stage_json")"
prepare_error="$(jq -r '.data.error // empty' <<<"$stage_json")"
echo "status=$prepare_status"
echo "duration=$(duration_human "$prepare_duration_ms") (${prepare_duration_ms}ms)"
if [[ -n "$prepare_error" ]]; then
  echo "error=$prepare_error"
fi
echo

echo "== Prepare bottleneck lines =="
if ! grep -E 'Allocation completed|Start services|Service .* started|launch docker|docker pull|Pulling fs layer|Waiting|Verifying Checksum|Download complete|Pull complete|Finished, code|context canceled|Pipeline prepare error' "$prepare_log" | tail -n "$tail_lines"; then
  echo "(no prepare bottleneck lines matched)"
fi
echo

echo "== Prepare log tail =="
tail -n "$tail_lines" "$prepare_log"
echo

runner_json="$tmpdir/runner_download_log.json"
runner_log="$tmpdir/runner_download.log"
if cnb build build-runner-download-log --repo "$repo" --pipelineId "$pipeline_id" --verbose >"$runner_json" 2>"$tmpdir/runner_download.err"; then
  if [[ "$(jq -r '.data.type // empty' "$runner_json")" == "base64" ]]; then
    jq -r '.data.data // empty' "$runner_json" | decode_base64_field >"$runner_log"
    echo "== Runner log long-wait candidates =="
    if ! grep -E '\+[0-9]+(\.[0-9]+)?m\]|docker pull|Pulling fs layer|Download complete|Pull complete|context canceled|duration:' "$runner_log" | tail -n "$tail_lines"; then
      echo "(no runner long-wait candidates matched)"
    fi
    echo
    echo "== Runner log tail =="
    tail -n "$tail_lines" "$runner_log"
  else
    echo "== Runner log =="
    echo "CNB did not return a base64 runner log payload."
    jq . "$runner_json"
  fi
else
  echo "== Runner log =="
  echo "Could not fetch runner download log:"
  cat "$tmpdir/runner_download.err"
fi

#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  bash scripts/watch_cnb_prepare.sh <SN> [PIPELINE_ID]

Environment:
  CNB_REPO=wuyoumusic/moss-music-runner
  CNB_WATCH_INTERVAL_SECONDS=5

This is a read-only local watcher. It does not start or stop CNB workspaces.
Run it immediately after creating a Dev GPU workspace to see prepare-stage
events such as runner allocation, git checkout, service startup, and Docker
image pulls before the WebIDE/log viewer is available.
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

latest_pipeline_id() {
  local status_json
  if ! status_json="$(cnb build get-build-status --repo "$repo" --sn "$sn" --verbose 2>&1)"; then
    echo "[watch] get-build-status failed while looking for pipeline id:" >&2
    echo "$status_json" >&2
    return 1
  fi
  if [[ -n "${pipeline_id:-}" ]]; then
    printf '%s' "$status_json" | python3 scripts/cnb_pipeline_selector.py --pipeline-id "$pipeline_id"
  else
    printf '%s' "$status_json" | python3 scripts/cnb_pipeline_selector.py
  fi
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

sn="${1:-}"
pipeline_id="${2:-}"
repo="${CNB_REPO:-wuyoumusic/moss-music-runner}"
interval="${CNB_WATCH_INTERVAL_SECONDS:-5}"

if [[ -z "$sn" ]]; then
  usage >&2
  exit 2
fi

if [[ ! "$interval" =~ ^[0-9]+$ || "$interval" -lt 1 ]]; then
  echo "CNB_WATCH_INTERVAL_SECONDS must be a positive integer." >&2
  exit 2
fi

require_command cnb
require_command jq
require_command python3

echo "[watch] repo=$repo"
echo "[watch] sn=$sn"

# Validate an operator-provided id against build status before requesting its
# stage. Without this guard a typo would retry a nonexistent stage forever.
if [[ -n "$pipeline_id" ]]; then
  selector_error_file="$(mktemp)"
  if pipeline_id="$(latest_pipeline_id 2>"$selector_error_file")"; then
    rm -f "$selector_error_file"
  else
    selector_error="$(cat "$selector_error_file")"
    rm -f "$selector_error_file"
    echo "[watch] could not select pipeline: ${selector_error:-unknown error}" >&2
    exit 2
  fi
fi

while [[ -z "$pipeline_id" ]]; do
  selector_error_file="$(mktemp)"
  if pipeline_id="$(latest_pipeline_id 2>"$selector_error_file")"; then
    rm -f "$selector_error_file"
    :
  else
    selector_error="$(cat "$selector_error_file")"
    rm -f "$selector_error_file"
    # A build can legitimately have no pipeline immediately after trigger. An
    # ambiguous multi-pipeline build is an operator error and must not silently
    # attach to an arbitrary pipeline.
    if [[ "$selector_error" == *"No pipeline id found"* ]]; then
      echo "[watch] pipeline id not visible yet; polling again in ${interval}s..."
      sleep "$interval"
      continue
    fi
    echo "[watch] could not select pipeline: ${selector_error:-unknown error}" >&2
    exit 2
  fi
done

echo "[watch] pipeline_id=$pipeline_id"
echo "[watch] polling prepare stage every ${interval}s"
echo

seen_lines=0
last_status=""

while :; do
  timestamp="$(date '+%Y-%m-%d %H:%M:%S')"
  if ! stage_json="$(cnb build get-build-stage --repo "$repo" --sn "$sn" --pipelineId "$pipeline_id" --stageId prepare --verbose 2>&1)"; then
    echo "[$timestamp] prepare request failed; retrying in ${interval}s"
    echo "$stage_json"
    sleep "$interval"
    continue
  fi

  if ! jq -e . >/dev/null 2>&1 <<<"$stage_json"; then
    echo "[$timestamp] prepare response is not JSON; retrying in ${interval}s"
    echo "$stage_json"
    sleep "$interval"
    continue
  fi

  status="$(jq -r '.data.status // "unknown"' <<<"$stage_json")"
  duration_ms="$(jq -r '.data.duration // 0' <<<"$stage_json")"
  line_count="$(jq -r '(.data.content // []) | length' <<<"$stage_json")"
  error_text="$(jq -r '.data.error // empty' <<<"$stage_json")"

  if ((line_count < seen_lines)); then
    seen_lines=0
  fi

  echo "[$timestamp] prepare status=$status duration=$(duration_human "$duration_ms") content_lines=$line_count"

  if ((line_count > seen_lines)); then
    jq -r --argjson start "$seen_lines" '(.data.content // [])[$start:][]?' <<<"$stage_json"
    seen_lines="$line_count"
  else
    echo "[watch] no new prepare log lines"
  fi

  if [[ -n "$error_text" && "$error_text" != "null" ]]; then
    echo "[watch] prepare error: $error_text"
  fi

  if [[ "$status" != "$last_status" ]]; then
    last_status="$status"
  fi

  case "$status" in
    success)
      echo
      echo "[watch] terminal_status=success"
      exit 0
      ;;
    error|cancel|canceled|failed|skipped)
      echo
      echo "[watch] terminal_status=$status" >&2
      exit 1
      ;;
  esac

  echo
  sleep "$interval"
done

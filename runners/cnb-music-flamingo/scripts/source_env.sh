#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

AUDIO_ROOT="${AUDIO_ROOT:-$ROOT/data/input}"
WORK_DIR="${WORK_DIR:-$ROOT/data/output/music_flamingo_pipeline}"
MUSIC_FLAMINGO_OUTPUT_NAME="${MUSIC_FLAMINGO_OUTPUT_NAME:-one_smoke}"
MUSIC_FLAMINGO_RUN_ID="${MUSIC_FLAMINGO_RUN_ID:-${CNB_BUILD_ID:-${CNB_PIPELINE_ID:-local}}}"

export AUDIO_ROOT
export WORK_DIR
export MUSIC_FLAMINGO_OUTPUT_NAME
export MUSIC_FLAMINGO_RUN_ID
export INF_API_KEY="${INF_API_KEY:-}"
if [ -n "${HF_ENDPOINT:-}" ]; then
  export HF_ENDPOINT
else
  unset HF_ENDPOINT
fi

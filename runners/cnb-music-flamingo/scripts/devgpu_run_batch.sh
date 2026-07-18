#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export HF_HOME="${HF_HOME:-/opt/huggingface}"
export MUSIC_FLAMINGO_MODEL="${MUSIC_FLAMINGO_MODEL:-nvidia/music-flamingo-think-2601-hf}"
export MUSIC_FLAMINGO_REVISION="${MUSIC_FLAMINGO_REVISION:-1ea2109}"
export MUSIC_FLAMINGO_MODEL_DIR="${MUSIC_FLAMINGO_MODEL_DIR:-/opt/models/music-flamingo-think-2601-hf}"
export MUSIC_FLAMINGO_MAX_NEW_TOKENS="${MUSIC_FLAMINGO_MAX_NEW_TOKENS:-2048}"
export MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS="${MUSIC_FLAMINGO_AUDIO_CLIP_SECONDS:-240}"
export MUSIC_FLAMINGO_BATCH_LIMIT="${MUSIC_FLAMINGO_BATCH_LIMIT:-10}"
export MUSIC_FLAMINGO_OUTPUT_NAME="${MUSIC_FLAMINGO_OUTPUT_NAME:-devgpu_batch}"
export MUSIC_FLAMINGO_RUN_ID="${MUSIC_FLAMINGO_RUN_ID:-${CNB_BUILD_ID:-${CNB_PIPELINE_ID:-local}}}"
export AUDIO_ROOT="${AUDIO_ROOT:-data/input/batch10}"
export WORK_DIR="${WORK_DIR:-data/output/music_flamingo_pipeline}"
export CNB_RUNTIME_IMAGE="${CNB_RUNTIME_IMAGE:-${CNB_DOCKER_REGISTRY:-docker.cnb.cool}/${CNB_REPO_SLUG_LOWERCASE:-wuyoumusic/moss-music-runner}:music-flamingo-cuda-model}"

# exec keeps CNB cancellation signals attached to the Python supervisor, which
# atomically records success, failure, or interruption for the log viewer.
exec python scripts/devgpu_batch_supervisor.py

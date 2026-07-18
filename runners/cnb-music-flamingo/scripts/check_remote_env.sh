#!/usr/bin/env bash
set -euo pipefail

echo "== CNB remote environment =="
date
pwd

echo
echo "== System =="
uname -a

echo
echo "== Python =="
python3 --version || true
python --version || true

echo
echo "== GPU =="
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi
else
  echo "nvidia-smi not found"
fi

echo
echo "== Disk =="
df -h .

echo
echo "== Key tools =="
for bin in git curl ffmpeg ffprobe; do
  if command -v "$bin" >/dev/null 2>&1; then
    echo "$bin: $(command -v "$bin")"
  else
    echo "$bin: missing"
  fi
done

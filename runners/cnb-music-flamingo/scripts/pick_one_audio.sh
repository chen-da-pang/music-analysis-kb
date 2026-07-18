#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/source_env.sh"

run_dir="$(python "$ROOT/scripts/music_flamingo_run_context.py" print-dir)"
mkdir -p "$run_dir"

python - "$AUDIO_ROOT" "$run_dir/one_audio.jsonl" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
out = Path(sys.argv[2])
exts = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus"}
files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts)
if not files:
    raise SystemExit(f"No audio files found under {root}")

chosen = files[0]
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({"audio_path": str(chosen), "info": chosen.stem}, ensure_ascii=False) + "\n", encoding="utf-8")
print(chosen)
print(out)
PY

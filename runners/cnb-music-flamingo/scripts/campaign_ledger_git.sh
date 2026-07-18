#!/usr/bin/env bash
# Persist the small, append-only campaign result ledger on a dedicated Git ref.
# The source checkout's Docker volume is node-local, so it must never be the
# only recovery copy for a multi-pipeline campaign.
set -euo pipefail

usage() {
  echo "Usage: $0 restore|checkpoint LEDGER_PATH" >&2
  exit 2
}

[[ $# -eq 2 ]] || usage
action="$1"
ledger_path="$2"
case "$action" in
  restore|checkpoint) ;;
  *) usage ;;
esac

root="$(cd "$(dirname "$0")/.." && pwd)"
repo_url="${MUSIC_FLAMINGO_LEDGER_REPO_URL:?MUSIC_FLAMINGO_LEDGER_REPO_URL is required}"
branch="${MUSIC_FLAMINGO_LEDGER_BRANCH:?MUSIC_FLAMINGO_LEDGER_BRANCH is required}"
user_name="${MUSIC_FLAMINGO_LEDGER_GIT_USER_NAME:-CNB Music Campaign Ledger}"
user_email="${MUSIC_FLAMINGO_LEDGER_GIT_USER_EMAIL:-cnb-ledger@wuyoumusic.invalid}"

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/music-flamingo-ledger.XXXXXX")"
askpass=""
cleanup() {
  rm -rf "$work_dir"
  if [[ -n "$askpass" ]]; then
    rm -f "$askpass"
  fi
}
trap cleanup EXIT

# A local file remote is useful for tests.  CNB HTTPS remotes require the
# short-lived CI token, supplied through askpass rather than a logged URL.
if [[ "$repo_url" == https://* || "$repo_url" == http://* ]]; then
  : "${CNB_TOKEN:?CNB_TOKEN is required for the HTTPS ledger repository}"
  askpass="$(mktemp "${TMPDIR:-/tmp}/music-flamingo-git-askpass.XXXXXX")"
  cat > "$askpass" <<'ASKPASS'
#!/bin/sh
case "$1" in
  *Username*|*username*) printf '%s\n' cnb ;;
  *Password*|*password*) printf '%s\n' "$CNB_TOKEN" ;;
  *) exit 1 ;;
esac
ASKPASS
  chmod 700 "$askpass"
fi

run_git() {
  if [[ -n "$askpass" ]]; then
    GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0 git "$@"
  else
    GIT_TERMINAL_PROMPT=0 git "$@"
  fi
}

run_git clone --quiet --depth 1 --branch "$branch" "$repo_url" "$work_dir/repo"
ledger_repo="$work_dir/repo"
remote_ledger="$ledger_repo/campaign_ledger.jsonl"
[[ -f "$remote_ledger" ]] || { echo "Ledger branch is missing campaign_ledger.jsonl" >&2; exit 2; }

validate_ledger() {
  PYTHONPATH="$root/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 - "$1" <<'PYTHON'
import sys
from pathlib import Path
from music_flamingo_campaign import read_campaign_ledger

records = read_campaign_ledger(Path(sys.argv[1]))
print(f"ledger_record_count={len(records)}")
PYTHON
}

case "$action" in
  restore)
    validate_ledger "$remote_ledger"
    mkdir -p "$(dirname "$ledger_path")"
    temporary="$(dirname "$ledger_path")/.${ledger_path##*/}.restore.$$"
    cp "$remote_ledger" "$temporary"
    validate_ledger "$temporary" >/dev/null
    mv -f "$temporary" "$ledger_path"
    # Sync the restored directory entry as well as the destination file.
    LEDGER_PATH="$ledger_path" python3 - <<'PYTHON'
import os
from pathlib import Path

path = Path(os.environ["LEDGER_PATH"])
with path.open("rb") as handle:
    os.fsync(handle.fileno())
directory_fd = os.open(str(path.parent), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PYTHON
    echo "campaign_ledger_restored=$ledger_path"
    ;;
  checkpoint)
    [[ -f "$ledger_path" ]] || { echo "Ledger does not exist: $ledger_path" >&2; exit 2; }
    validate_ledger "$ledger_path"
    cp "$ledger_path" "$remote_ledger"
    ledger_sha256="$(shasum -a 256 "$remote_ledger" | awk '{print $1}')"
    record_count="$(PYTHONPATH="$root/scripts${PYTHONPATH:+:$PYTHONPATH}" python3 - "$remote_ledger" <<'PYTHON'
import sys
from pathlib import Path
from music_flamingo_campaign import read_campaign_ledger
print(len(read_campaign_ledger(Path(sys.argv[1]))))
PYTHON
)"
    LEDGER_SHA256="$ledger_sha256" LEDGER_RECORD_COUNT="$record_count" python3 - > "$ledger_repo/campaign_state.json" <<'PYTHON'
import json
import os
import time

print(json.dumps({
    "schema_version": 1,
    "ledger_sha256": os.environ["LEDGER_SHA256"],
    "ledger_record_count": int(os.environ["LEDGER_RECORD_COUNT"]),
    "checkpointed_at_epoch_seconds": round(time.time(), 3),
}, sort_keys=True))
PYTHON
    run_git -C "$ledger_repo" config user.name "$user_name"
    run_git -C "$ledger_repo" config user.email "$user_email"
    # Dev GPU workspaces can inherit CNB's global commit.gpgSign=true setting,
    # but the temporary ledger clone has no corresponding signing key.  Its
    # authenticated push is the durability boundary, so explicitly prevent an
    # inherited signing policy from turning a completed inference item into a
    # node-local-only result.
    run_git -C "$ledger_repo" config commit.gpgSign false
    run_git -C "$ledger_repo" add campaign_ledger.jsonl campaign_state.json
    if run_git -C "$ledger_repo" diff --cached --quiet; then
      echo "campaign_ledger_checkpoint_unchanged=$ledger_path"
      exit 0
    fi
    run_git -C "$ledger_repo" commit --quiet -m "checkpoint: Music Flamingo campaign ledger"
    # Never force-push.  The campaign pipeline lock makes a non-fast-forward
    # update a hard signal that a second writer appeared unexpectedly.
    run_git -C "$ledger_repo" push origin "HEAD:refs/heads/$branch"
    echo "campaign_ledger_checkpointed=$ledger_path"
    ;;
esac

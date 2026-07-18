#!/usr/bin/env python3
"""Prepare the no-results queue and execute the fixed fallback worker through Claude Code."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from music_kb.operation_context import load_validated_operations, sha256_file


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_checked(command: list[str], cwd: Path, timeout: int) -> dict:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {result.stderr[-2000:]}")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--proxy")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--operations-file", type=Path, default=Path(__file__).resolve().parents[1] / "references" / "validated-operations.json")
    parser.add_argument("--profile", type=Path, default=Path(__file__).resolve().parents[1] / "references" / "fallback-download-profile.json")
    args = parser.parse_args()
    started_at = now_iso()
    workspace = args.workspace.expanduser().resolve()
    operations = args.operations_file.expanduser().resolve()
    profile = args.profile.expanduser().resolve()
    load_validated_operations(operations, required_atom="fallback_download")
    run_dir = workspace / "data" / "download_runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    queue = run_dir / "fallback_queue.jsonl"
    inventory = workspace / "data" / "song_inventory.json"
    progress = run_dir / "fallback-progress.json"
    scripts = Path(__file__).resolve().parent
    manifest = run_checked([sys.executable, str(scripts / "prepare_fallback_queue.py"), "--inventory", str(inventory), "--output", str(queue), "--profile", str(profile)], workspace, args.timeout_seconds)
    (run_dir / "fallback-queue-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    values = ["python3", str(scripts / "download_music_fallback.py"), "--queue", str(queue), "--inventory", str(inventory), "--work-dir", str(workspace / "music_downloads" / "KugouMusicClient"), "--progress", str(progress), "--run-id", args.run_id, "--profile", str(profile)]
    if args.dry_run:
        values.append("--dry-run")
    command = " ".join(shlex.quote(value) for value in values)
    prompt = "\n".join([
        "你是音乐库 fallback 下载原子的 Claude Code 执行器。",
        "只运行下面固定命令；不得改脚本、队列、inventory，不得调用 kugou-cli 或旧 batch_download.py。",
        "worker 会按 QQ、咪咕、酷我串行搜索；必须等待它退出。",
        command,
        "完成后只返回 worker 的 JSON summary。",
    ]) + "\n"
    (run_dir / "claude_prompt.txt").write_text(prompt, encoding="utf-8")
    env = os.environ.copy()
    if args.proxy:
        env["http_proxy"] = args.proxy
        env["https_proxy"] = args.proxy
    result = subprocess.run([args.claude_bin, "-p", "--output-format", "json", "--permission-mode", "dontAsk", "--allowedTools", "Bash", "Read", "--add-dir", str(workspace)], cwd=workspace, env=env, input=prompt, capture_output=True, text=True, timeout=args.timeout_seconds, check=False)
    (run_dir / "claude_stdout.json").write_text(result.stdout, encoding="utf-8")
    (run_dir / "claude_stderr.log").write_text(result.stderr, encoding="utf-8")
    summary = {"run_id": args.run_id, "queue_manifest": manifest, "claude_exit_code": result.returncode, "progress": str(progress), "operations_sha256": sha256_file(operations), "profile": str(profile)}
    if progress.exists():
        progress_data = json.loads(progress.read_text(encoding="utf-8"))
        summary["worker_progress"] = progress_data
        worker_summary = progress_data.get("summary", {})
        processed = sum(int(worker_summary.get(key, 0)) for key in ("downloaded", "skipped_existing", "failed", "no_results"))
        if not args.dry_run and (not progress_data.get("finished_at") or processed != manifest["queued"]):
            summary["progress_incomplete"] = {"queued": manifest["queued"], "processed": processed}
            result.returncode = 2
    elif manifest["queued"] and not args.dry_run and result.returncode == 0:
        summary["progress_missing"] = True
        result.returncode = 2
    summary["claude_exit_code"] = result.returncode
    receipt = {
        "status": "succeeded" if result.returncode == 0 else "failed",
        "started_at": started_at,
        "finished_at": now_iso(),
        "run_id": args.run_id,
        "atom": "fallback_download",
        "inputs": {"inventory": str(inventory), "profile": str(profile), "queue": str(queue)},
        "outputs": summary,
        "operations_file": str(operations),
        "operations_sha256": sha256_file(operations),
        "command": values,
    }
    receipt_path = workspace / "data" / "weekly_runs" / args.run_id / "atoms" / "fallback_download.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary["receipt"] = str(receipt_path)
    print(json.dumps(summary, ensure_ascii=False))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

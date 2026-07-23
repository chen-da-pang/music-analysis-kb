#!/usr/bin/env python3
"""Prepare the no-results queue and execute the fixed fallback worker.

The default direct path keeps one worker in charge of inventory and progress.
``--executor claude`` remains available only for a bounded compatibility retry.
"""

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
    parser.add_argument(
        "--executor",
        choices=("direct", "claude"),
        default="direct",
        help="direct runs the fixed fallback worker (default); claude preserves the legacy executor",
    )
    parser.add_argument(
        "--worker-python",
        default=os.environ.get("MUSICDL_PYTHON", "python3"),
        help="Python executable with musicdl for the fallback worker",
    )
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
    values = [args.worker_python, str(scripts / "download_music_fallback.py"), "--queue", str(queue), "--inventory", str(inventory), "--work-dir", str(workspace / "music_downloads" / "KugouMusicClient"), "--progress", str(progress), "--run-id", args.run_id, "--profile", str(profile)]
    if args.dry_run:
        values.append("--dry-run")
    env = os.environ.copy()
    if args.proxy:
        env["http_proxy"] = args.proxy
        env["https_proxy"] = args.proxy

    worker_exit_code = 0
    claude_exit_code: int | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    if manifest["queued"] == 0 and not args.dry_run:
        execution = "skipped_empty_queue"
    elif args.executor == "direct":
        execution = "direct"
        stdout_path = run_dir / "worker_stdout.json"
        stderr_path = run_dir / "worker_stderr.log"
        result = subprocess.run(
            values,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=args.timeout_seconds,
            check=False,
        )
        worker_exit_code = result.returncode
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
    else:
        execution = "claude"
        command = " ".join(shlex.quote(value) for value in values)
        prompt = "\n".join([
            "你是音乐库 fallback 下载原子的 Claude Code 执行器。",
            "只运行下面固定命令；不得改脚本、队列、inventory，不得调用 kugou-cli 或旧 batch_download.py。",
            "worker 会按 QQ、咪咕、酷我串行搜索；必须等待它退出。",
            command,
            "完成后只返回 worker 的 JSON summary。",
        ]) + "\n"
        (run_dir / "claude_prompt.txt").write_text(prompt, encoding="utf-8")
        stdout_path = run_dir / "claude_stdout.json"
        stderr_path = run_dir / "claude_stderr.log"
        result = subprocess.run(
            [args.claude_bin, "-p", "--output-format", "json", "--permission-mode", "dontAsk", "--allowedTools", "Bash", "Read", "--add-dir", str(workspace)],
            cwd=workspace,
            env=env,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=args.timeout_seconds,
            check=False,
        )
        worker_exit_code = result.returncode
        claude_exit_code = result.returncode
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")

    summary = {
        "run_id": args.run_id,
        "queue_manifest": manifest,
        "executor": args.executor,
        "execution": execution,
        "worker_python": args.worker_python,
        "worker_exit_code": worker_exit_code,
        "claude_exit_code": claude_exit_code,
        "stdout": str(stdout_path) if stdout_path is not None else None,
        "stderr": str(stderr_path) if stderr_path is not None else None,
        "progress": str(progress),
        "operations_sha256": sha256_file(operations),
        "profile": str(profile),
    }
    if progress.exists():
        progress_data = json.loads(progress.read_text(encoding="utf-8"))
        summary["worker_progress"] = progress_data
        worker_summary = progress_data.get("summary", {})
        processed = sum(int(worker_summary.get(key, 0)) for key in ("downloaded", "skipped_existing", "failed", "no_results"))
        if not args.dry_run and (not progress_data.get("finished_at") or processed != manifest["queued"]):
            summary["progress_incomplete"] = {"queued": manifest["queued"], "processed": processed}
            worker_exit_code = 2
    elif manifest["queued"] and not args.dry_run and worker_exit_code == 0:
        summary["progress_missing"] = True
        worker_exit_code = 2
    summary["worker_exit_code"] = worker_exit_code
    if args.executor == "claude":
        summary["claude_exit_code"] = worker_exit_code
    receipt = {
        "status": "succeeded" if worker_exit_code == 0 else "failed",
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
    return worker_exit_code


if __name__ == "__main__":
    raise SystemExit(main())

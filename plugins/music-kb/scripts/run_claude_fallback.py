#!/usr/bin/env python3
"""Prepare the retryable cross-platform queue and execute the fixed worker through Claude Code."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
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


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON receipt must be an object: {path}")
    return value


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_completion(launch: dict, completion_path: Path, timeout_seconds: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if completion_path.is_file():
            return load_json(completion_path)
        pid = launch.get("supervisor_pid")
        if not isinstance(pid, int) or pid <= 0:
            raise RuntimeError("launcher receipt has no valid supervisor pid")
        if not process_alive(pid):
            raise RuntimeError("detached fallback supervisor exited without a completion receipt")
        time.sleep(1)
    raise RuntimeError(f"detached fallback worker timed out after {timeout_seconds}s")


def resolve_musicdl_python(explicit: str | None) -> str:
    candidates: list[str] = []
    for candidate in (explicit, os.environ.get("MUSICDL_PYTHON"), sys.executable, shutil.which("python3")):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    micromamba_root = Path.home() / ".local" / "share" / "micromamba" / "envs"
    if micromamba_root.is_dir():
        for candidate in sorted(micromamba_root.glob("*/bin/python3")):
            rendered = str(candidate)
            if rendered not in candidates:
                candidates.append(rendered)
    checked: list[str] = []
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if not path.is_file():
            continue
        try:
            result = subprocess.run(
                [str(path), "-c", "import musicdl"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except OSError:
            continue
        checked.append(str(path))
        if result.returncode == 0:
            return str(path.resolve())
    raise RuntimeError(
        "no Python interpreter can import musicdl; supply --worker-python or MUSICDL_PYTHON "
        f"(checked: {', '.join(checked) or 'none'})"
    )


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
    parser.add_argument("--retry-statuses", help="Override comma-separated retry statuses from the fallback profile")
    parser.add_argument("--worker-python", help="Python interpreter that imports musicdl; defaults to MUSICDL_PYTHON or a validated local interpreter")
    args = parser.parse_args()
    started_at = now_iso()
    workspace = args.workspace.expanduser().resolve()
    operations = args.operations_file.expanduser().resolve()
    profile = args.profile.expanduser().resolve()
    load_validated_operations(operations, required_atom="fallback_download")
    worker_python = resolve_musicdl_python(args.worker_python)
    profile_data = json.loads(profile.read_text(encoding="utf-8"))
    retry_statuses = args.retry_statuses or ",".join(profile_data.get("retry_statuses", ["no_results", "failed"]))
    run_dir = workspace / "data" / "download_runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    queue = run_dir / "fallback_queue.jsonl"
    inventory = workspace / "data" / "song_inventory.json"
    progress = run_dir / "fallback-progress.json"
    launch_receipt = run_dir / "fallback-worker-launch.json"
    completion_receipt = run_dir / "fallback-worker-completion.json"
    worker_log = run_dir / "fallback-worker.log"
    scripts = Path(__file__).resolve().parent
    manifest = run_checked([
        sys.executable,
        str(scripts / "prepare_fallback_queue.py"),
        "--inventory",
        str(inventory),
        "--output",
        str(queue),
        "--profile",
        str(profile),
        "--statuses",
        retry_statuses,
    ], workspace, args.timeout_seconds)
    (run_dir / "fallback-queue-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    values = [
        "python3",
        str(scripts / "launch_music_fallback_worker.py"),
        "--queue",
        str(queue),
        "--inventory",
        str(inventory),
        "--work-dir",
        str(workspace / "music_downloads" / "KugouMusicClient"),
        "--progress",
        str(progress),
        "--run-id",
        args.run_id,
        "--profile",
        str(profile),
        "--launch-receipt",
        str(launch_receipt),
        "--completion-receipt",
        str(completion_receipt),
        "--worker-log",
        str(worker_log),
        "--worker-python",
        worker_python,
    ]
    if args.dry_run:
        values.append("--dry-run")
    command = " ".join(shlex.quote(value) for value in values)
    prompt = "\n".join([
        "你是音乐库 fallback 下载原子的 Claude Code 执行器。",
        "只且仅一次运行下面固定的短命令；不得改脚本、队列、inventory，不得调用 kugou-cli 或旧 batch_download.py。",
        f"该命令会启动只处理 {','.join(manifest['retry_statuses'])} 的 detached worker；它按 QQ、咪咕、酷我串行搜索。命令返回后不得等待、kill、重试、包装或再次运行 worker。",
        command,
        "该 launcher 返回 JSON 后立刻结束；只返回该 JSON。",
    ]) + "\n"
    (run_dir / "claude_prompt.txt").write_text(prompt, encoding="utf-8")
    env = os.environ.copy()
    if args.proxy:
        env["http_proxy"] = args.proxy
        env["https_proxy"] = args.proxy
    claude_exit_code: int | None = None
    claude_error: str | None = None
    claude_stdout = ""
    claude_stderr = ""
    try:
        result = subprocess.run(
            [args.claude_bin, "-p", "--output-format", "json", "--permission-mode", "dontAsk", "--allowedTools", "Bash", "Read", "--add-dir", str(workspace)],
            cwd=workspace,
            env=env,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=min(args.timeout_seconds, 600),
            check=False,
        )
        claude_exit_code = result.returncode
        claude_stdout = result.stdout
        claude_stderr = result.stderr
    except subprocess.TimeoutExpired as exc:
        claude_error = f"Claude launcher timed out after 600s: {exc}"
        claude_stdout = exc.stdout or ""
        claude_stderr = exc.stderr or ""
    (run_dir / "claude_stdout.json").write_text(claude_stdout, encoding="utf-8")
    (run_dir / "claude_stderr.log").write_text(claude_stderr, encoding="utf-8")
    summary = {
        "run_id": args.run_id,
        "queue_manifest": manifest,
        "claude_exit_code": claude_exit_code,
        "claude_error": claude_error,
        "progress": str(progress),
        "operations_sha256": sha256_file(operations),
        "profile": str(profile),
        "worker_python": worker_python,
    }
    atom_exit_code = 0
    if launch_receipt.is_file():
        launch = load_json(launch_receipt)
        summary["worker_launch"] = launch
        try:
            completion = wait_for_completion(launch, completion_receipt, args.timeout_seconds)
            summary["worker_completion"] = completion
            if completion.get("status") != "succeeded" or completion.get("exit_code") != 0:
                atom_exit_code = 2
        except RuntimeError as exc:
            summary["worker_completion_error"] = str(exc)
            atom_exit_code = 2
    else:
        summary["launch_missing"] = True
        atom_exit_code = 2
    if progress.exists():
        progress_data = json.loads(progress.read_text(encoding="utf-8"))
        summary["worker_progress"] = progress_data
        worker_summary = progress_data.get("summary", {})
        processed = sum(int(worker_summary.get(key, 0)) for key in ("downloaded", "skipped_existing", "failed", "no_results"))
        if not args.dry_run and (not progress_data.get("finished_at") or processed != manifest["queued"]):
            summary["progress_incomplete"] = {"queued": manifest["queued"], "processed": processed}
            atom_exit_code = 2
    elif manifest["queued"] and not args.dry_run:
        summary["progress_missing"] = True
        atom_exit_code = 2
    summary["atom_exit_code"] = atom_exit_code
    receipt = {
        "status": "succeeded" if atom_exit_code == 0 else "failed",
        "started_at": started_at,
        "finished_at": now_iso(),
        "run_id": args.run_id,
        "atom": "fallback_download",
        "inputs": {"inventory": str(inventory), "profile": str(profile), "queue": str(queue), "retry_statuses": manifest["retry_statuses"], "worker_python": worker_python},
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
    return atom_exit_code


if __name__ == "__main__":
    raise SystemExit(main())

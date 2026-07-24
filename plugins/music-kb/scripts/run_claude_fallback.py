#!/usr/bin/env python3
"""Prepare retryable fallback work and launch an isolated detached supervisor.

The normal direct path starts the short launcher itself.  The launcher owns one
or two private fallback shards and writes a completion receipt only after its
serial merger has safely updated durable state.  ``--executor claude`` remains
an explicit compatibility path which asks Claude Code to start that same short
launcher; Claude never owns the long-running download process.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from music_kb.operation_context import load_validated_operations, sha256_file


SUMMARY_KEYS = ("downloaded", "skipped_existing", "failed", "no_results")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_checked(command: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {result.stderr[-2000:]}")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("command did not emit a JSON object")
    return value


def load_json(path: Path) -> dict[str, Any]:
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


def wait_for_completion(launch: dict[str, Any], completion_path: Path, timeout_seconds: int) -> dict[str, Any]:
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


def launcher_values(
    *,
    scripts: Path,
    workspace: Path,
    run_dir: Path,
    queue: Path,
    inventory: Path,
    work_dir: Path,
    progress: Path,
    run_id: str,
    profile: Path,
    launch_receipt: Path,
    completion_receipt: Path,
    worker_log: Path,
    worker_python: str,
    parallelism: int,
    timeout_seconds: int,
    proxy: str | None,
    dry_run: bool,
) -> list[str]:
    values = [
        sys.executable,
        str(scripts / "launch_music_fallback_worker.py"),
        "--workspace",
        str(workspace),
        "--run-dir",
        str(run_dir),
        "--queue",
        str(queue),
        "--inventory",
        str(inventory),
        "--work-dir",
        str(work_dir),
        "--progress",
        str(progress),
        "--run-id",
        run_id,
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
        "--parallelism",
        str(parallelism),
        "--timeout-seconds",
        str(timeout_seconds),
    ]
    if proxy:
        values.extend(["--proxy", proxy])
    if dry_run:
        values.append("--dry-run")
    return values


def launch_with_claude(
    *,
    args: argparse.Namespace,
    workspace: Path,
    command: list[str],
    run_dir: Path,
    manifest: dict[str, Any],
    env: dict[str, str],
) -> tuple[int | None, str | None, Path, Path]:
    rendered = " ".join(shlex.quote(value) for value in command)
    prompt = "\n".join(
        [
            "你是音乐库 fallback 下载原子的 Claude Code 启动器。",
            "只且仅一次运行下面固定的短 launcher 命令；不得改脚本、队列、inventory，不得调用 kugou-cli 或旧 batch_download.py。",
            f"该命令启动只处理 {','.join(manifest['retry_statuses'])} 的 detached supervisor。命令返回后不得等待、kill、重试、包装或再次运行 worker。",
            rendered,
            "launcher 返回 JSON 后立刻结束；只返回该 JSON。",
        ]
    ) + "\n"
    (run_dir / "claude_prompt.txt").write_text(prompt, encoding="utf-8")
    stdout_path = run_dir / "claude_stdout.json"
    stderr_path = run_dir / "claude_stderr.log"
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
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        return result.returncode, None, stdout_path, stderr_path
    except subprocess.TimeoutExpired as exc:
        stdout_path.write_text(exc.stdout or "", encoding="utf-8")
        stderr_path.write_text(exc.stderr or "", encoding="utf-8")
        return None, f"Claude launcher timed out after 600s: {exc}", stdout_path, stderr_path


def processed_count(progress: dict[str, Any]) -> int:
    summary = progress.get("summary", {})
    return sum(int(summary.get(key, 0)) for key in SUMMARY_KEYS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--executor", choices=("direct", "claude"), default="direct")
    parser.add_argument("--parallelism", type=int, choices=(1, 2), default=2)
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
    profile_data = load_json(profile)
    retry_statuses = args.retry_statuses or ",".join(profile_data.get("retry_statuses", ["no_results", "failed"]))
    run_dir = workspace / "data" / "download_runs" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    queue = run_dir / "fallback_queue.jsonl"
    inventory = workspace / "data" / "song_inventory.json"
    progress = run_dir / "fallback-progress.json"
    work_dir = workspace / "music_downloads" / "KugouMusicClient"
    launch_receipt = run_dir / "fallback-worker-launch.json"
    completion_receipt = run_dir / "fallback-worker-completion.json"
    worker_log = run_dir / "fallback-worker.log"
    manifest_path = run_dir / "fallback-queue-manifest.json"
    scripts = Path(__file__).resolve().parent
    resumed = launch_receipt.is_file()
    if resumed:
        if not manifest_path.is_file():
            raise RuntimeError(f"cannot resume fallback launch without queue manifest: {manifest_path}")
        manifest = load_json(manifest_path)
    else:
        manifest = run_checked(
            [
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
            ],
            workspace,
            args.timeout_seconds,
        )
        atomic_write_json(manifest_path, manifest)
    values = launcher_values(
        scripts=scripts,
        workspace=workspace,
        run_dir=run_dir,
        queue=queue,
        inventory=inventory,
        work_dir=work_dir,
        progress=progress,
        run_id=args.run_id,
        profile=profile,
        launch_receipt=launch_receipt,
        completion_receipt=completion_receipt,
        worker_log=worker_log,
        worker_python=worker_python,
        parallelism=args.parallelism,
        timeout_seconds=args.timeout_seconds,
        proxy=args.proxy,
        dry_run=args.dry_run,
    )
    env = os.environ.copy()
    if args.proxy:
        env["http_proxy"] = args.proxy
        env["https_proxy"] = args.proxy

    launcher_exit_code: int | None = None
    claude_exit_code: int | None = None
    launcher_error: str | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    if resumed:
        execution = "resumed_detached"
    elif int(manifest["queued"]) == 0 and not args.dry_run:
        execution = "skipped_empty_queue"
    elif args.executor == "direct":
        execution = "direct_detached"
        stdout_path = run_dir / "launcher_stdout.json"
        stderr_path = run_dir / "launcher_stderr.log"
        try:
            result = subprocess.run(
                values,
                cwd=workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=min(args.timeout_seconds, 600),
                check=False,
            )
            launcher_exit_code = result.returncode
            stdout_path.write_text(result.stdout, encoding="utf-8")
            stderr_path.write_text(result.stderr, encoding="utf-8")
        except subprocess.TimeoutExpired as exc:
            launcher_error = f"direct launcher timed out after 600s: {exc}"
            stdout_path.write_text(exc.stdout or "", encoding="utf-8")
            stderr_path.write_text(exc.stderr or "", encoding="utf-8")
    else:
        execution = "claude_detached"
        claude_exit_code, launcher_error, stdout_path, stderr_path = launch_with_claude(
            args=args,
            workspace=workspace,
            command=values,
            run_dir=run_dir,
            manifest=manifest,
            env=env,
        )
        launcher_exit_code = claude_exit_code

    summary: dict[str, Any] = {
        "run_id": args.run_id,
        "queue_manifest": manifest,
        "executor": args.executor,
        "execution": execution,
        "requested_parallelism": args.parallelism,
        "worker_python": worker_python,
        "launcher_exit_code": launcher_exit_code,
        "claude_exit_code": claude_exit_code,
        "launcher_error": launcher_error,
        "stdout": str(stdout_path) if stdout_path else None,
        "stderr": str(stderr_path) if stderr_path else None,
        "progress": str(progress),
        "operations_sha256": sha256_file(operations),
        "profile": str(profile),
    }
    atom_exit_code = 0
    if execution != "skipped_empty_queue":
        if launch_receipt.is_file():
            launch = load_json(launch_receipt)
            summary["worker_launch"] = launch
            try:
                completion = wait_for_completion(launch, completion_receipt, args.timeout_seconds)
                summary["worker_completion"] = completion
                summary["parallelism"] = completion.get("parallelism")
                if completion.get("parallel") is not None:
                    summary["parallel"] = completion["parallel"]
                if completion.get("status") != "succeeded" or completion.get("exit_code") != 0:
                    atom_exit_code = 2
            except RuntimeError as exc:
                summary["worker_completion_error"] = str(exc)
                atom_exit_code = 2
        else:
            summary["launch_missing"] = True
            atom_exit_code = 2
    if launcher_exit_code not in (None, 0):
        atom_exit_code = 2
    if launcher_error:
        atom_exit_code = 2
    if progress.exists():
        progress_data = load_json(progress)
        summary["worker_progress"] = progress_data
        if not args.dry_run and (not progress_data.get("finished_at") or processed_count(progress_data) != int(manifest["queued"])):
            summary["progress_incomplete"] = {"queued": manifest["queued"], "processed": processed_count(progress_data)}
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
        "inputs": {
            "inventory": str(inventory),
            "profile": str(profile),
            "queue": str(queue),
            "retry_statuses": manifest["retry_statuses"],
            "worker_python": worker_python,
        },
        "outputs": summary,
        "operations_file": str(operations),
        "operations_sha256": sha256_file(operations),
        "command": values,
    }
    receipt_path = workspace / "data" / "weekly_runs" / args.run_id / "atoms" / "fallback_download.json"
    atomic_write_json(receipt_path, receipt)
    summary["receipt"] = str(receipt_path)
    print(json.dumps(summary, ensure_ascii=False))
    return atom_exit_code


if __name__ == "__main__":
    raise SystemExit(main())

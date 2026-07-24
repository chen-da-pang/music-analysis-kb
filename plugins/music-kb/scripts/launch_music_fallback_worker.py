#!/usr/bin/env python3
"""Launch state-isolated fallback workers outside a Claude Code Bash lifetime."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fallback_parallel


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


def worker_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    if args.proxy:
        env["http_proxy"] = args.proxy
        env["https_proxy"] = args.proxy
    return env


def worker_command(args: argparse.Namespace) -> list[str]:
    """The one-item worker command used only for a non-mutating dry-run."""
    command = [
        args.worker_python,
        str(SCRIPTS_DIR / "download_music_fallback.py"),
        "--queue",
        str(args.queue),
        "--inventory",
        str(args.inventory),
        "--work-dir",
        str(args.work_dir),
        "--progress",
        str(args.progress),
        "--run-id",
        args.run_id,
        "--profile",
        str(args.profile),
    ]
    if args.dry_run:
        command.append("--dry-run")
    return command


def supervisor_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--supervise",
        "--workspace",
        str(args.workspace),
        "--run-dir",
        str(args.run_dir),
        "--queue",
        str(args.queue),
        "--inventory",
        str(args.inventory),
        "--work-dir",
        str(args.work_dir),
        "--progress",
        str(args.progress),
        "--run-id",
        args.run_id,
        "--profile",
        str(args.profile),
        "--launch-receipt",
        str(args.launch_receipt),
        "--completion-receipt",
        str(args.completion_receipt),
        "--worker-log",
        str(args.worker_log),
        "--worker-python",
        args.worker_python,
        "--parallelism",
        str(args.parallelism),
        "--timeout-seconds",
        str(args.timeout_seconds),
    ]
    if args.proxy:
        command.extend(["--proxy", args.proxy])
    if args.dry_run:
        command.append("--dry-run")
    return command


def supervise(args: argparse.Namespace) -> int:
    """Run the real worker(s), then write one durable completion receipt."""
    started_at = now_iso()
    log_path = args.worker_log
    command: list[str] | list[list[str]] = []
    parallel_state: dict[str, Any] | None = None
    exit_code = 127
    error: str | None = None
    try:
        if args.dry_run:
            command = worker_command(args)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("w", encoding="utf-8") as log_handle:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    env=worker_env(args),
                    check=False,
                )
            exit_code = result.returncode
        else:
            controller_args = SimpleNamespace(
                parallelism=args.parallelism,
                worker_python=args.worker_python,
                timeout_seconds=args.timeout_seconds,
                run_id=args.run_id,
            )
            exit_code, parallel_state = fallback_parallel.execute_isolated_parallel(
                args=controller_args,
                scripts=SCRIPTS_DIR,
                workspace=args.workspace,
                run_dir=args.run_dir,
                queue=args.queue,
                inventory=args.inventory,
                work_dir=args.work_dir,
                progress=args.progress,
                profile=args.profile,
                env=worker_env(args),
                started_at=started_at,
            )
            command = parallel_state.get("commands", [])
            atomic_write_json(
                log_path,
                {
                    "schema_version": 1,
                    "run_id": args.run_id,
                    "parallelism": args.parallelism,
                    "exit_code": exit_code,
                    "parallel": parallel_state,
                },
            )
    except Exception as exc:  # pragma: no cover - defensive process boundary
        error = f"{type(exc).__name__}: {exc}"
        atomic_write_json(log_path, {"schema_version": 1, "run_id": args.run_id, "error": error})
    completion = {
        "schema_version": 1,
        "status": "succeeded" if exit_code == 0 else "failed",
        "started_at": started_at,
        "finished_at": now_iso(),
        "run_id": args.run_id,
        "supervisor_pid": os.getpid(),
        "parallelism": args.parallelism,
        "worker_command": command,
        "worker_log": str(log_path.resolve()),
        "worker_python": args.worker_python,
        "exit_code": exit_code,
        "error": error,
    }
    if parallel_state is not None:
        completion["parallel"] = parallel_state
    atomic_write_json(args.completion_receipt, completion)
    return exit_code


def launch(args: argparse.Namespace) -> int:
    launch_path = args.launch_receipt
    if launch_path.exists():
        raise SystemExit(f"launch receipt already exists; refusing a duplicate worker: {launch_path}")
    command = supervisor_command(args)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=worker_env(args),
        start_new_session=True,
    )
    receipt = {
        "schema_version": 1,
        "status": "launched",
        "launched_at": now_iso(),
        "run_id": args.run_id,
        "supervisor_pid": process.pid,
        "supervisor_command": command,
        "completion_receipt": str(args.completion_receipt.resolve()),
        "worker_log": str(args.worker_log.resolve()),
        "worker_python": args.worker_python,
        "parallelism": args.parallelism,
    }
    atomic_write_json(launch_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, help="Worker cwd; defaults from the inventory location for legacy launcher calls")
    parser.add_argument("--run-dir", type=Path, help="Run-local shard directory; defaults to the launch receipt directory")
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--launch-receipt", type=Path, required=True)
    parser.add_argument("--completion-receipt", type=Path, required=True)
    parser.add_argument("--worker-log", type=Path, required=True)
    parser.add_argument("--worker-python", required=True, help="Python interpreter already verified to import musicdl")
    parser.add_argument("--parallelism", type=int, choices=(1, 2), default=2)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--proxy")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    for attribute in (
        "queue",
        "inventory",
        "work_dir",
        "progress",
        "profile",
        "launch_receipt",
        "completion_receipt",
        "worker_log",
    ):
        setattr(args, attribute, getattr(args, attribute).expanduser().resolve())
    if args.workspace is None:
        args.workspace = (args.inventory.parent.parent if args.inventory.parent.name == "data" else args.inventory.parent).resolve()
    else:
        args.workspace = args.workspace.expanduser().resolve()
    args.run_dir = (args.run_dir or args.launch_receipt.parent).expanduser().resolve()
    return supervise(args) if args.supervise else launch(args)


if __name__ == "__main__":
    raise SystemExit(main())

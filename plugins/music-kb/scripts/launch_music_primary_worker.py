#!/usr/bin/env python3
"""Launch the isolated primary-download supervisor.

The short launcher is the command Claude Code executes.  It immediately
detaches a supervisor, writes a launch receipt, and exits.  The supervisor
owns the long-running two-shard download and writes a completion receipt only
after the serial merger has committed formal state.
"""

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

import primary_parallel


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
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
    command = primary_parallel.worker_values(
        worker_python=args.worker_python,
        scripts=SCRIPTS_DIR,
        queue=args.queue,
        inventory=args.inventory,
        work_dir=args.work_dir,
        progress=args.progress,
        log=args.worker_log,
        lyrics_receipt=args.lyrics_receipt,
        run_id=args.run_id,
        item_timeout_seconds=args.item_timeout_seconds,
        lookup_mode=args.lookup_mode,
        worker_delay=args.worker_delay,
    )
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
        "--lyrics-receipt",
        str(args.lyrics_receipt),
        "--run-id",
        args.run_id,
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
        "--item-timeout-seconds",
        str(args.item_timeout_seconds),
        "--lookup-mode",
        args.lookup_mode,
        "--worker-delay",
        str(args.worker_delay),
    ]
    if args.proxy:
        command.extend(["--proxy", args.proxy])
    if args.dry_run:
        command.append("--dry-run")
    return command


def supervise(args: argparse.Namespace) -> int:
    started_at = now_iso()
    command: list[str] | list[list[str]] = []
    parallel_state: dict[str, Any] | None = None
    exit_code = 127
    error: str | None = None
    try:
        if args.dry_run:
            command = worker_command(args)
            args.worker_log.parent.mkdir(parents=True, exist_ok=True)
            with args.worker_log.open("w", encoding="utf-8") as log_handle:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    env=worker_env(args),
                    cwd=args.workspace,
                    check=False,
                )
            exit_code = result.returncode
        else:
            controller_args = SimpleNamespace(
                parallelism=args.parallelism,
                worker_python=args.worker_python,
                timeout_seconds=args.timeout_seconds,
                run_id=args.run_id,
                item_timeout_seconds=args.item_timeout_seconds,
                lookup_mode=args.lookup_mode,
                worker_delay=args.worker_delay,
            )
            exit_code, parallel_state = primary_parallel.execute_isolated_parallel(
                args=controller_args,
                scripts=SCRIPTS_DIR,
                workspace=args.workspace,
                run_dir=args.run_dir,
                queue=args.queue,
                inventory=args.inventory,
                work_dir=args.work_dir,
                progress=args.progress,
                lyrics_receipt=args.lyrics_receipt,
                env=worker_env(args),
                started_at=started_at,
            )
            atomic_write_json(
                args.worker_log,
                {
                    "schema_version": 1,
                    "run_id": args.run_id,
                    "parallelism": args.parallelism,
                    "exit_code": exit_code,
                    "parallel": parallel_state,
                },
            )
            command = parallel_state.get("commands", []) if parallel_state else []
    except Exception as exc:  # pragma: no cover - defensive process boundary
        error = f"{type(exc).__name__}: {exc}"
        atomic_write_json(args.worker_log, {"schema_version": 1, "run_id": args.run_id, "error": error})

    completion: dict[str, Any] = {
        "schema_version": 1,
        "status": "succeeded" if exit_code == 0 else "failed",
        "started_at": started_at,
        "finished_at": now_iso(),
        "run_id": args.run_id,
        "supervisor_pid": os.getpid(),
        "parallelism": args.parallelism,
        "worker_command": command,
        "worker_log": str(args.worker_log.resolve()),
        "worker_python": args.worker_python,
        "exit_code": exit_code,
        "error": error,
    }
    if parallel_state is not None:
        completion["parallel"] = parallel_state
    atomic_write_json(args.completion_receipt, completion)
    return exit_code


def launch(args: argparse.Namespace) -> int:
    if args.launch_receipt.exists():
        raise SystemExit(f"launch receipt already exists; refusing a duplicate worker: {args.launch_receipt}")
    command = supervisor_command(args)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=worker_env(args),
        cwd=args.workspace,
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
    atomic_write_json(args.launch_receipt, receipt)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--lyrics-receipt", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--launch-receipt", type=Path, required=True)
    parser.add_argument("--completion-receipt", type=Path, required=True)
    parser.add_argument("--worker-log", type=Path, required=True)
    parser.add_argument("--worker-python", required=True)
    parser.add_argument("--parallelism", type=int, choices=(1, 2), default=2)
    parser.add_argument("--timeout-seconds", type=int, default=86_400)
    parser.add_argument("--item-timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--lookup-mode",
        choices=("exact-page-first", "search-only"),
        default="search-only",
        help="Use strict title/artist search with exact MixSongID validation (default); exact-page-first is diagnostic only.",
    )
    parser.add_argument("--worker-delay", type=float, default=0.0)
    parser.add_argument("--proxy")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    for attribute in (
        "workspace",
        "run_dir",
        "queue",
        "inventory",
        "work_dir",
        "progress",
        "lyrics_receipt",
        "launch_receipt",
        "completion_receipt",
        "worker_log",
    ):
        setattr(args, attribute, getattr(args, attribute).expanduser().resolve())
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be positive")
    if args.item_timeout_seconds <= 0:
        parser.error("--item-timeout-seconds must be positive")
    if args.worker_delay < 0:
        parser.error("--worker-delay cannot be negative")
    return supervise(args) if args.supervise else launch(args)


if __name__ == "__main__":
    raise SystemExit(main())

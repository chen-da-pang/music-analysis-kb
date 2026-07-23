#!/usr/bin/env python3
"""Launch the fixed fallback worker outside the lifetime of a Claude Code Bash call."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def worker_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve().with_name("download_music_fallback.py")),
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
    ]
    if args.dry_run:
        command.append("--dry-run")
    return command


def supervise(args: argparse.Namespace) -> int:
    started_at = now_iso()
    command = worker_command(args)
    log_path = args.worker_log
    exit_code = 127
    error: str | None = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log_handle:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                check=False,
            )
        exit_code = result.returncode
    except Exception as exc:  # pragma: no cover - defensive process boundary
        error = str(exc)
    completion = {
        "schema_version": 1,
        "status": "succeeded" if exit_code == 0 else "failed",
        "started_at": started_at,
        "finished_at": now_iso(),
        "run_id": args.run_id,
        "supervisor_pid": os.getpid(),
        "worker_command": command,
        "worker_log": str(log_path.resolve()),
        "exit_code": exit_code,
        "error": error,
    }
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
    }
    atomic_write_json(launch_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--launch-receipt", type=Path, required=True)
    parser.add_argument("--completion-receipt", type=Path, required=True)
    parser.add_argument("--worker-log", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    for attribute in ("queue", "inventory", "work_dir", "progress", "profile", "launch_receipt", "completion_receipt", "worker_log"):
        setattr(args, attribute, getattr(args, attribute).expanduser().resolve())
    return supervise(args) if args.supervise else launch(args)


if __name__ == "__main__":
    raise SystemExit(main())

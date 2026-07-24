#!/usr/bin/env python3
"""Orchestrate inventory -> dedup queue -> fixed audio-worker execution.

The direct executor is the normal path: it runs one serial
``download_music_queue.py`` process so its inventory/progress/lyric-receipt
writes remain owned by exactly one worker. ``--executor claude`` preserves the
older bounded Claude Code chunk path for an explicit compatibility retry.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from music_kb.operation_context import load_validated_operations, sha256_file


DEFAULT_CHUNK_SIZE = 8
TERMINAL_STATUSES = {"downloaded", "skipped_existing", "failed", "no_results"}


def now_run_id(source: Path) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"kugou-download-{stamp}-{source.stem[:32]}"


def run_checked(command: list[str], cwd: Path, *, timeout_seconds: int) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout_seconds)
    if result.returncode:
        raise RuntimeError(f"命令失败 ({result.returncode}): {' '.join(command)}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"queue row must be an object: {path}")
            rows.append(value)
    return rows


def row_identity(row: dict[str, Any]) -> str:
    identity = str(row.get("identity_key") or row.get("title_artist_key") or "").strip()
    if not identity:
        raise ValueError("queue row has no stable identity key")
    return identity


def load_progress(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"results": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {"results": {}}


def _inventory_download_resolved(
    identity: str, inventory: dict[str, Any] | None, audio_root: Path | None
) -> bool:
    if not isinstance(inventory, dict):
        return False
    for song in inventory.get("songs", []):
        if not isinstance(song, dict) or song.get("identity_key") != identity:
            continue
        download = song.get("download") if isinstance(song.get("download"), dict) else {}
        if download.get("retention") == "purged_after_analysis":
            return True
        raw_path = download.get("path")
        if download.get("status") != "downloaded" or not raw_path:
            return False
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute() and audio_root is not None:
            path = audio_root / path
        return path.is_file()
    return False


def _progress_identity_resolved(
    identity: str, progress: dict[str, Any], inventory: dict[str, Any] | None, audio_root: Path | None
) -> bool:
    results = progress.get("results") if isinstance(progress.get("results"), dict) else {}
    result = results.get(identity) if isinstance(results.get(identity), dict) else {}
    status = result.get("status")
    if status in {"failed", "no_results"}:
        return True
    if status in {"downloaded", "skipped_existing"}:
        if inventory is None:
            return True
        return _inventory_download_resolved(identity, inventory, audio_root)
    return False


def pending_chunks(
    rows: list[dict[str, Any]],
    progress: dict[str, Any],
    chunk_size: int,
    max_items: int | None,
    inventory: dict[str, Any] | None = None,
    audio_root: Path | None = None,
) -> list[list[dict[str, Any]]]:
    pending = [
        row
        for row in rows
        if not _progress_identity_resolved(row_identity(row), progress, inventory, audio_root)
    ]
    if max_items is not None:
        pending = pending[:max_items]
    return [pending[index : index + chunk_size] for index in range(0, len(pending), chunk_size)]


def pending_rows(
    rows: list[dict[str, Any]],
    progress: dict[str, Any],
    max_items: int | None,
    inventory: dict[str, Any] | None = None,
    audio_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Select one safe serial execution queue without exposing shared state."""

    return [
        row
        for row in rows
        if not _progress_identity_resolved(row_identity(row), progress, inventory, audio_root)
    ][:max_items]


def write_queue_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def elapsed_ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000, 3)


def aggregate_progress(
    rows: list[dict[str, Any]],
    progress: dict[str, Any],
    inventory: dict[str, Any] | None = None,
    audio_root: Path | None = None,
) -> dict[str, Any]:
    results = progress.get("results") if isinstance(progress.get("results"), dict) else {}
    counts = Counter()
    unresolved: list[str] = []
    for row in rows:
        identity = row_identity(row)
        status = results.get(identity, {}).get("status") if isinstance(results.get(identity), dict) else None
        resolved = status in {"failed", "no_results"} or (
            status in {"downloaded", "skipped_existing"}
            and (inventory is None or _inventory_download_resolved(identity, inventory, audio_root))
        )
        if resolved and status in TERMINAL_STATUSES:
            counts[str(status)] += 1
        else:
            unresolved.append(identity)
    return {
        "queue": len(rows),
        "downloaded": counts["downloaded"],
        "skipped_existing": counts["skipped_existing"],
        "failed": counts["failed"],
        "no_results": counts["no_results"],
        "unresolved": unresolved,
    }


def render_prompt(worker_command: str, *, chunk_index: int, chunk_total: int) -> str:
    prompt_lines = [
        "你是本周音乐库下载原子的 Claude Code 执行器。",
        f"这是串行小批次 {chunk_index}/{chunk_total}；只执行当前队列，不要自行改写下载器，不要调用 kugou-cli，也不要重新抓榜单。",
        "历史有效方法是 musicdl 的 MusicClient + KugouMusicClient：标题和歌手只能定位候选；只有固定 worker 验证 Kugou MixSongID 与队列平台 ID 一致才可下载或采集歌词。",
        "严格运行下面这一条命令；只允许读写这些绝对路径。队列为空时不要初始化 musicdl，直接报告没有新增下载。",
        "绝不能手工修改 inventory、progress、queue、歌词 receipt 或音频状态；只能让固定 worker 写入。不得把未产生文件的歌曲标记为 downloaded、purged_after_analysis 或 skipped。",
        "worker 会写结构化歌词 receipt；不得扫描、移动或手工补写 .lrc，网络/解析/identity 失败必须保留 pending，不能伪造成平台无歌词。",
        "单曲超时或 musicdl 返回失败时保留真实 failed/no_results 记录并继续队列；不要重写命令、不要手工跳过、不要伪造成功。",
        "必须等待 worker 命令真正退出，并确认 progress.json 有本批次的终端结果；不得在命令仍运行时提前返回。若 Bash 将长进程转为后台，只使用一次 Monitor 等待它结束（timeout 至少 600000ms）；禁止用 Bash while/kill/sleep 轮询，也不要重复启动 worker。",
        worker_command,
        "命令完成后读取 stdout 和 progress.json，只返回 JSON 摘要；不要运行旧的 batch_download.py。",
    ]
    return "\n".join(prompt_lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True, help="kugou-cli 处理后的 songs JSON/JSONL/CSV")
    parser.add_argument("--run-id")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument(
        "--executor",
        choices=("direct", "claude"),
        default="direct",
        help="direct runs one fixed serial worker (default); claude preserves the legacy chunk executor",
    )
    parser.add_argument(
        "--worker-python",
        default=os.environ.get("MUSICDL_PYTHON", "python3"),
        help="Python executable with musicdl for the fixed worker (default: $MUSICDL_PYTHON or python3)",
    )
    parser.add_argument(
        "--lookup-mode",
        choices=("exact-page-first", "search-only"),
        default="exact-page-first",
        help="Use the queue's exact Kugou mix-song page before legacy title/artist search (default).",
    )
    parser.add_argument("--model")
    parser.add_argument("--max-budget-usd", type=float)
    parser.add_argument("--timeout-seconds", type=int, default=86_400)
    parser.add_argument(
        "--operations-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "references" / "validated-operations.json",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-items", type=int)
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="Maximum queue rows per legacy Claude Code session (serial; default: 8)",
    )
    parser.add_argument(
        "--reuse-queue",
        action="store_true",
        help="Reuse the existing run queue/manifest during a same-run download resume",
    )
    parser.add_argument(
        "--item-timeout-seconds",
        type=float,
        default=60.0,
        help="Maximum seconds for one musicdl search or download operation",
    )
    parser.add_argument("--hash-inventory", action="store_true")
    parser.add_argument("--proxy", help="例如 http://127.0.0.1:7890；会传给固定 worker 或 Claude Code 子进程")
    args = parser.parse_args()

    workspace = args.workspace.expanduser().resolve()
    source = args.source.expanduser().resolve()
    operations_file = args.operations_file.expanduser().resolve()
    load_validated_operations(operations_file, required_atom="claude_download")
    run_id = args.run_id or now_run_id(source)
    run_dir = workspace / "data" / "download_runs" / run_id
    inventory = workspace / "data" / "song_inventory.json"
    queue = run_dir / "download_queue.jsonl"
    manifest = run_dir / "queue_manifest.json"
    progress = run_dir / "progress.json"
    log = run_dir / "download.log"
    lyrics_receipt = run_dir / "lyrics-receipts.jsonl"
    db = workspace / "data" / "music_trends.sqlite"
    legacy_progress = workspace / "download_progress.json"
    audio_root = workspace / "music_downloads" / "KugouMusicClient"
    work_dir = workspace / "music_downloads"
    scripts_dir = Path(__file__).resolve().parent
    build_script = scripts_dir / "build_song_inventory.py"
    queue_script = scripts_dir / "prepare_download_queue.py"
    worker_script = scripts_dir / "download_music_queue.py"

    if args.timeout_seconds < 1:
        raise ValueError("timeout-seconds must be positive")
    if args.item_timeout_seconds <= 0:
        raise ValueError("item-timeout-seconds must be positive")
    if args.chunk_size < 1:
        raise ValueError("chunk-size must be positive")
    if args.max_items is not None and args.max_items < 1:
        raise ValueError("max-items must be positive when supplied")

    run_started = time.monotonic()
    run_dir.mkdir(parents=True, exist_ok=True)
    build_command = [
        sys.executable,
        str(build_script),
        "--db",
        str(db),
        "--progress",
        str(legacy_progress),
        "--inventory",
        str(inventory),
        "--audio-root",
        str(audio_root),
    ]
    if args.hash_inventory:
        build_command.append("--hash")
    inventory_started = time.monotonic()
    inventory_output = run_checked(build_command, workspace, timeout_seconds=args.timeout_seconds)
    inventory_build_ms = elapsed_ms(inventory_started)

    queue_prepare_ms = 0.0
    if args.reuse_queue:
        if not queue.is_file() or not manifest.is_file():
            raise RuntimeError("--reuse-queue requires the existing queue and queue_manifest.json")
        queue_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    else:
        queue_started = time.monotonic()
        queue_output = run_checked(
            [
                sys.executable,
                str(queue_script),
                "--source",
                str(source),
                "--inventory",
                str(inventory),
                "--output",
                str(queue),
                "--audio-root",
                str(audio_root),
            ],
            workspace,
            timeout_seconds=args.timeout_seconds,
        )
        queue_prepare_ms = elapsed_ms(queue_started)
        queue_manifest = json.loads(queue_output)
        manifest.write_text(json.dumps(queue_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    selection_started = time.monotonic()
    rows = load_jsonl_rows(queue)
    if int(queue_manifest.get("queued", len(rows))) != len(rows):
        raise RuntimeError("queue manifest count does not match queue rows")
    progress_before = load_progress(progress)
    inventory_before = json.loads(inventory.read_text(encoding="utf-8"))
    selected_rows = pending_rows(rows, progress_before, args.max_items, inventory_before, audio_root)
    legacy_chunks = [
        selected_rows[index : index + args.chunk_size]
        for index in range(0, len(selected_rows), args.chunk_size)
    ]
    selection_ms = elapsed_ms(selection_started)
    deferred_rows = max(0, len(pending_rows(rows, progress_before, None, inventory_before, audio_root)) - len(selected_rows))
    timing = {
        "schema_version": 1,
        "inventory_build_ms": inventory_build_ms,
        "queue_prepare_ms": queue_prepare_ms,
        "selection_ms": selection_ms,
    }
    if args.dry_run:
        summary = {
            "run_id": run_id,
            "queue": len(rows),
            "would_process": len(selected_rows),
            "queued_for_attempt": len(selected_rows),
            "remaining_after_limit": deferred_rows,
            "chunk_size": args.chunk_size,
            "chunks": len(legacy_chunks),
            "executor": args.executor,
            "worker_python": args.worker_python,
            "lookup_mode": args.lookup_mode,
            "reuse_queue": args.reuse_queue,
            "lyrics_receipt": str(lyrics_receipt),
            "timing": {**timing, "total_ms": elapsed_ms(run_started)},
            "dry_run": True,
        }
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    env = os.environ.copy()
    if args.proxy:
        env["https_proxy"] = args.proxy
        env["http_proxy"] = args.proxy

    def worker_values(worker_queue: Path) -> list[str]:
        return [
            args.worker_python,
            str(worker_script),
            "--queue",
            str(worker_queue),
            "--inventory",
            str(inventory),
            "--work-dir",
            str(work_dir),
            "--progress",
            str(progress),
            "--log",
            str(log),
            "--run-id",
            run_id,
            "--lyrics-receipt",
            str(lyrics_receipt),
            "--item-timeout-seconds",
            str(args.item_timeout_seconds),
            "--lookup-mode",
            args.lookup_mode,
        ]

    chunk_receipts: list[dict[str, Any]] = []
    worker_elapsed_ms = 0.0
    worker_exit_code = 0
    worker_stdout: Path | None = None
    worker_stderr: Path | None = None
    last_claude_stdout: Path | None = None
    last_claude_stderr: Path | None = None
    exit_code = 0

    if not selected_rows:
        chunk_receipts.append(
            {
                "executor": args.executor,
                "chunk_index": 0,
                "chunk_count": 0,
                "skipped": "no pending queue rows; musicdl was not initialized",
            }
        )
    elif args.executor == "direct":
        direct_queue = run_dir / "download-queue-direct.jsonl"
        worker_stdout = run_dir / "worker_stdout.json"
        worker_stderr = run_dir / "worker_stderr.log"
        write_queue_rows(direct_queue, selected_rows)
        worker_started = time.monotonic()
        completed = subprocess.run(
            worker_values(direct_queue),
            cwd=workspace,
            env=env,
            text=True,
            capture_output=True,
            timeout=args.timeout_seconds,
            check=False,
        )
        worker_elapsed_ms = elapsed_ms(worker_started)
        worker_exit_code = completed.returncode
        worker_stdout.write_text(completed.stdout, encoding="utf-8")
        worker_stderr.write_text(completed.stderr, encoding="utf-8")
        progress_after = load_progress(progress)
        inventory_after = json.loads(inventory.read_text(encoding="utf-8"))
        direct_aggregate = aggregate_progress(selected_rows, progress_after, inventory_after, audio_root)
        complete = not direct_aggregate["unresolved"] and bool(progress_after.get("finished_at"))
        chunk_receipts.append(
            {
                "executor": "direct",
                "chunk_index": 1,
                "chunk_count": len(selected_rows),
                "queue": str(direct_queue),
                "stdout": str(worker_stdout),
                "stderr": str(worker_stderr),
                "worker_exit_code": completed.returncode,
                "elapsed_ms": worker_elapsed_ms,
                "complete": complete and completed.returncode == 0,
                "aggregate": direct_aggregate,
            }
        )
        if completed.returncode != 0 or not complete:
            exit_code = 2
    else:
        claude_command = [
            args.claude_bin,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "Bash",
            "Read",
            "Monitor",
            "--add-dir",
            str(workspace),
        ]
        if args.model:
            claude_command.extend(["--model", args.model])
        if args.max_budget_usd is not None:
            claude_command.extend(["--max-budget-usd", str(args.max_budget_usd)])

        for chunk_index, chunk in enumerate(legacy_chunks, start=1):
            chunk_queue = run_dir / f"download-queue-{chunk_index:04d}.jsonl"
            write_queue_rows(chunk_queue, chunk)
            worker_command = " ".join(shlex.quote(value) for value in worker_values(chunk_queue))
            prompt = render_prompt(worker_command, chunk_index=chunk_index, chunk_total=len(legacy_chunks))
            prompt_path = run_dir / f"claude_prompt-{chunk_index:04d}.txt"
            stdout_path = run_dir / f"claude_stdout-{chunk_index:04d}.json"
            stderr_path = run_dir / f"claude_stderr-{chunk_index:04d}.log"
            prompt_path.write_text(prompt, encoding="utf-8")
            chunk_started = time.monotonic()
            completed = subprocess.run(
                claude_command,
                cwd=workspace,
                env=env,
                text=True,
                input=prompt,
                capture_output=True,
                timeout=args.timeout_seconds,
                check=False,
            )
            chunk_elapsed_ms = elapsed_ms(chunk_started)
            worker_elapsed_ms += chunk_elapsed_ms
            worker_exit_code = completed.returncode
            stdout_path.write_text(completed.stdout, encoding="utf-8")
            stderr_path.write_text(completed.stderr, encoding="utf-8")
            last_claude_stdout = run_dir / "claude_stdout.json"
            last_claude_stderr = run_dir / "claude_stderr.log"
            last_claude_stdout.write_text(completed.stdout, encoding="utf-8")
            last_claude_stderr.write_text(completed.stderr, encoding="utf-8")
            progress_after = load_progress(progress)
            inventory_after = json.loads(inventory.read_text(encoding="utf-8"))
            chunk_aggregate = aggregate_progress(chunk, progress_after, inventory_after, audio_root)
            chunk_complete = not chunk_aggregate["unresolved"] and bool(progress_after.get("finished_at"))
            receipt = {
                "executor": "claude",
                "chunk_index": chunk_index,
                "chunk_count": len(chunk),
                "queue": str(chunk_queue),
                "prompt": str(prompt_path),
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "claude_exit_code": completed.returncode,
                "elapsed_ms": chunk_elapsed_ms,
                "complete": chunk_complete and completed.returncode == 0,
                "aggregate": chunk_aggregate,
            }
            chunk_receipts.append(receipt)
            if not receipt["complete"]:
                exit_code = 2
                break

    final_progress = load_progress(progress)
    final_inventory = json.loads(inventory.read_text(encoding="utf-8"))
    execution_aggregate = aggregate_progress(selected_rows, final_progress, final_inventory, audio_root)
    queue_aggregate = aggregate_progress(rows, final_progress, final_inventory, audio_root)
    if execution_aggregate["unresolved"]:
        exit_code = 2
    summary = {
        "run_id": run_id,
        "inventory_output": json.loads(inventory_output),
        "queue_manifest": queue_manifest,
        "queue": len(rows),
        "queued_for_attempt": len(selected_rows),
        "remaining_after_limit": deferred_rows,
        "chunk_size": args.chunk_size,
        "chunks": chunk_receipts,
        "executor": args.executor,
        "worker_python": args.worker_python,
        "lookup_mode": args.lookup_mode,
        "reuse_queue": args.reuse_queue,
        "run_dir": str(run_dir),
        "worker_stdout": str(worker_stdout) if worker_stdout is not None else None,
        "worker_stderr": str(worker_stderr) if worker_stderr is not None else None,
        "claude_stdout": str(last_claude_stdout) if last_claude_stdout is not None else None,
        "claude_stderr": str(last_claude_stderr) if last_claude_stderr is not None else None,
        "operations_file": str(operations_file),
        "operations_sha256": sha256_file(operations_file),
        "worker_progress": execution_aggregate,
        "queue_progress": queue_aggregate,
        "worker_progress_incomplete": bool(execution_aggregate["unresolved"]),
        "lyrics_receipt": str(lyrics_receipt),
        "lyrics_receipt_exists": lyrics_receipt.is_file(),
        "worker_exit_code": worker_exit_code,
        "claude_exit_code": worker_exit_code if args.executor == "claude" else None,
        "timing": {
            **timing,
            "worker_ms": worker_elapsed_ms,
            "total_ms": elapsed_ms(run_started),
        },
    }
    print(json.dumps(summary, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

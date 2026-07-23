#!/usr/bin/env python3
"""Run the no-audio, exact-identity Kugou lyric backfill.

This is the historical companion to ``run_claude_download.py``. It reads the
publisher master only to materialize unresolved canonical source tracks, then
defaults to the fixed worker's direct MixSongID -> page hash -> lyric path.
``--executor claude`` retains the older Claude Code chunk executor for a
bounded compatibility retry. Neither mode rebuilds audio inventory or
downloads existing audio files.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from music_kb.lyrics_backfill import materialize_lyric_backfill_queue
from music_kb.operation_context import RunContext, atom, atomic_write_json
from music_kb.repository import MusicKBRepository


DEFAULT_CHUNK_SIZE = 8
TERMINAL_STATUSES = frozenset({"available", "instrumental", "platform_unavailable"})


def now_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"kugou-lyrics-backfill-{stamp}"


def load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"lyric backfill queue line {line_number} must be an object: {path}")
        rows.append(value)
    return rows


def row_identity(row: Mapping[str, Any]) -> str:
    value = str(row.get("source_track_id") or "").strip()
    if not value:
        raise ValueError("lyric backfill queue row has no source_track_id")
    return value


def _result_for(row: Mapping[str, Any], progress: Mapping[str, Any]) -> Mapping[str, Any]:
    results = progress.get("results") if isinstance(progress.get("results"), Mapping) else {}
    result = results.get(row_identity(row)) if isinstance(results, Mapping) else None
    return result if isinstance(result, Mapping) else {}


def pending_chunks(
    rows: Sequence[dict[str, Any]],
    progress: Mapping[str, Any],
    *,
    chunk_size: int,
    max_items: int | None,
) -> list[list[dict[str, Any]]]:
    """Retain pending rows, but never re-query receipt-backed terminal rows."""

    pending = [
        row
        for row in rows
        if str(_result_for(row, progress).get("lyric_status") or "") not in TERMINAL_STATUSES
    ]
    if max_items is not None:
        pending = pending[:max_items]
    return [pending[index : index + chunk_size] for index in range(0, len(pending), chunk_size)]


def progress_summary(rows: Sequence[Mapping[str, Any]], progress: Mapping[str, Any]) -> dict[str, Any]:
    statuses: Counter[str] = Counter()
    unattempted: list[str] = []
    attempted = 0
    for row in rows:
        result = _result_for(row, progress)
        if result.get("query_status") != "completed":
            unattempted.append(row_identity(row))
            continue
        attempted += 1
        statuses[str(result.get("lyric_status") or "pending")] += 1
    return {
        "queue": len(rows),
        "attempted": attempted,
        "available": statuses["available"],
        "instrumental": statuses["instrumental"],
        "platform_unavailable": statuses["platform_unavailable"],
        "pending": statuses["pending"],
        "unattempted": unattempted,
    }


def compact_progress_summary(summary: Mapping[str, Any], *, sample_limit: int = 20) -> dict[str, Any]:
    """Keep the run receipt reviewable when a bounded batch leaves a long tail."""

    unattempted = list(summary.get("unattempted") or [])
    return {
        **dict(summary),
        "unattempted": unattempted[:sample_limit],
        "unattempted_count": len(unattempted),
    }


def compact_import_summary(summary: Mapping[str, Any], *, sample_limit: int = 20) -> dict[str, Any]:
    """Keep receipt/state JSON bounded; the full per-row evidence is JSONL."""

    results = list(summary.get("results") or [])
    return {
        **{key: value for key, value in summary.items() if key != "results"},
        "result_count": len(results),
        "result_sample": results[:sample_limit],
        "results_truncated": len(results) > sample_limit,
    }


def _write_chunk_queue(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def render_prompt(worker_command: str, *, chunk_index: int, chunk_total: int) -> str:
    lines = [
        "你是音乐库历史歌词回填原子的 Claude Code 执行器。",
        f"这是串行小批次 {chunk_index}/{chunk_total}；只能执行当前队列，不能下载音频、不能修改库存、不能调用 kugou-cli。",
        "标题和歌手只用于发现候选；固定 worker 必须以返回的 Kugou MixSongID 与队列平台 ID 精确匹配。",
        "只能运行下面这一条固定命令；不得扫描、移动或手工补写 .lrc。",
        "网络、解析、空白或 identity 失败必须写 pending receipt，绝不能伪造成无歌词或纯音乐。",
        "必须等待命令退出；不得后台启动、重复启动或手动编辑 progress/receipt。",
        worker_command,
        "完成后只读取 stdout 和 progress.json，返回 JSON 摘要。",
    ]
    return "\n".join(lines) + "\n"


def _load_progress(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"results": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {"results": {}}


def _import_receipts(database: Path, receipt_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    with MusicKBRepository(database, read_only=False) as repository:
        if receipt_path.is_file() and receipt_path.stat().st_size:
            imported = repository.import_lyric_receipt_file(receipt_path)
        else:
            imported = {"status": "skipped", "reason": "worker did not write a lyric receipt"}
        coverage = repository.lyric_coverage()
        validation = repository.validate(require_lyrics=True)
    return imported, coverage, validation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True, help="Writable publisher master SQLite database")
    parser.add_argument(
        "--chart-db",
        type=Path,
        help=(
            "Authoritative Kugou chart SQLite for legacy source URL -> MixSongID resolution "
            "(default: <workspace>/data/music_trends.sqlite)"
        ),
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        help=(
            "Durable Kugou download inventory used only to recover an exact archived "
            "audio hash when an old mix-song page no longer exposes one "
            "(default: <workspace>/data/song_inventory.json)"
        ),
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument(
        "--executor",
        choices=("direct", "claude"),
        default="direct",
        help=(
            "direct queries each exact Kugou mix-song page locally (default); "
            "claude preserves the older Claude Code chunk executor"
        ),
    )
    parser.add_argument(
        "--worker-python",
        default=os.environ.get("MUSICDL_PYTHON", "python3"),
        help=(
            "Python executable for the worker. Direct mode needs only the standard library; "
            "the Claude/musicdl fallback needs musicdl installed. Defaults to $MUSICDL_PYTHON or python3."
        ),
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
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--reuse-queue", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--item-timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Seconds to wait between exact lyric requests (default: 0.3)",
    )
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--proxy")
    args = parser.parse_args()

    workspace = args.workspace.expanduser().resolve()
    database = args.db.expanduser().resolve()
    chart_database = (
        args.chart_db.expanduser().resolve()
        if args.chart_db is not None
        else workspace / "data" / "music_trends.sqlite"
    )
    inventory = (
        args.inventory.expanduser().resolve()
        if args.inventory is not None
        else workspace / "data" / "song_inventory.json"
    )
    operations_file = args.operations_file.expanduser().resolve()
    run_id = args.run_id or now_run_id()
    if args.timeout_seconds < 1:
        raise ValueError("timeout-seconds must be positive")
    if args.chunk_size < 1:
        raise ValueError("chunk-size must be positive")
    if args.item_timeout_seconds <= 0:
        raise ValueError("item-timeout-seconds must be positive")
    if args.delay < 0:
        raise ValueError("delay must be non-negative")
    if args.retries < 0:
        raise ValueError("retries must be non-negative")
    if args.max_items is not None and args.max_items < 1:
        raise ValueError("max-items must be positive when supplied")

    run_dir = workspace / "data" / "weekly_runs" / run_id
    work_dir = run_dir / "lyrics-backfill"
    queue_path = work_dir / "queue.jsonl"
    manifest_path = work_dir / "queue-manifest.json"
    progress_path = work_dir / "progress.json"
    receipt_path = work_dir / "lyrics-receipts.jsonl"
    log_path = work_dir / "lyrics-backfill.log"
    worker_path = Path(__file__).resolve().parent / "download_music_queue.py"
    work_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any]
    with RunContext(run_id=run_id, run_dir=run_dir, operations_file=operations_file) as context:
        with atom(
            context,
            "lyrics_backfill",
            inputs={
                "database": str(database),
                "chart_database": str(chart_database),
                "inventory": str(inventory),
                "workspace": str(workspace),
                "dry_run": args.dry_run,
                "max_items": args.max_items,
                "executor": args.executor,
                "worker_python": args.worker_python,
            },
        ) as outputs:
            if args.dry_run:
                invalid_lyric_repair = {
                    "status": "not_run",
                    "reason": "dry-run does not modify publisher lyric rows",
                }
            else:
                with MusicKBRepository(database, read_only=False) as repository:
                    invalid_lyric_repair = repository.repair_invalid_available_lyrics()
            if args.reuse_queue:
                if not queue_path.is_file() or not manifest_path.is_file():
                    raise RuntimeError("--reuse-queue requires an existing queue.jsonl and queue-manifest.json")
                queue_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(queue_manifest, dict):
                    raise RuntimeError("existing lyric backfill queue manifest must be an object")
            else:
                queue_manifest = materialize_lyric_backfill_queue(
                    database,
                    queue_path,
                    chart_database=chart_database,
                    inventory=inventory,
                )
                queue_manifest["run_id"] = run_id
                atomic_write_json(manifest_path, queue_manifest)
            rows = load_jsonl_rows(queue_path)
            if int(queue_manifest.get("queue_count", len(rows))) != len(rows):
                raise RuntimeError("lyric backfill queue manifest count does not match queue rows")
            progress_before = _load_progress(progress_path)
            chunks = pending_chunks(
                rows,
                progress_before,
                chunk_size=args.chunk_size,
                max_items=args.max_items,
            )
            selected_rows = [row for chunk in chunks for row in chunk]
            summary = {
                "run_id": run_id,
                "mode": "lyrics_backfill",
                "queue_manifest": queue_manifest,
                "queue": len(rows),
                "queued_for_attempt": len(selected_rows),
                "chunk_size": args.chunk_size,
                "chunks": len(chunks),
                "executor": args.executor,
                "reuse_queue": args.reuse_queue,
                "receipt": str(receipt_path),
                "progress": str(progress_path),
                "dry_run": args.dry_run,
                "invalid_lyric_repair": invalid_lyric_repair,
            }
            if args.dry_run:
                summary["would_process"] = len(selected_rows)
                summary["remaining_after_limit"] = max(0, len(rows) - len(selected_rows))
                outputs.update(summary)
            else:
                env = os.environ.copy()
                if args.proxy:
                    env["http_proxy"] = args.proxy
                    env["https_proxy"] = args.proxy
                chunk_receipts: list[dict[str, Any]] = []

                def worker_values(worker_queue: Path, *, cache_dir: Path) -> list[str]:
                    return [
                        args.worker_python,
                        str(worker_path),
                        "--queue",
                        str(worker_queue),
                        "--work-dir",
                        str(cache_dir),
                        "--progress",
                        str(progress_path),
                        "--log",
                        str(log_path),
                        "--run-id",
                        run_id,
                        "--lyrics-only",
                        "--lyrics-receipt",
                        str(receipt_path),
                        "--item-timeout-seconds",
                        str(args.item_timeout_seconds),
                        "--delay",
                        str(args.delay),
                        "--retries",
                        str(args.retries),
                    ]

                if args.executor == "direct":
                    direct_queue = work_dir / "direct-queue.jsonl"
                    stdout_path = work_dir / "direct-stdout.json"
                    stderr_path = work_dir / "direct-stderr.log"
                    _write_chunk_queue(direct_queue, selected_rows)
                    completed = subprocess.run(
                        worker_values(direct_queue, cache_dir=work_dir / "direct-lyrics-cache"),
                        cwd=workspace,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=args.timeout_seconds,
                        check=False,
                    )
                    stdout_path.write_text(completed.stdout, encoding="utf-8")
                    stderr_path.write_text(completed.stderr, encoding="utf-8")
                    direct_progress = progress_summary(selected_rows, _load_progress(progress_path))
                    chunk_receipts.append(
                        {
                            "executor": "direct",
                            "chunk_index": 1,
                            "chunk_count": len(selected_rows),
                            "queue": str(direct_queue),
                            "stdout": str(stdout_path),
                            "stderr": str(stderr_path),
                            "worker_exit_code": completed.returncode,
                            "worker_progress": direct_progress,
                        }
                    )
                    if completed.returncode != 0:
                        raise RuntimeError("direct exact-source lyric backfill worker failed")
                    if direct_progress["unattempted"]:
                        raise RuntimeError(
                            "direct lyric worker did not produce progress: "
                            f"{direct_progress['unattempted']}"
                        )
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
                    for chunk_index, chunk in enumerate(chunks, start=1):
                        chunk_queue = work_dir / f"queue-{chunk_index:04d}.jsonl"
                        prompt_path = work_dir / f"claude-prompt-{chunk_index:04d}.txt"
                        stdout_path = work_dir / f"claude-stdout-{chunk_index:04d}.json"
                        stderr_path = work_dir / f"claude-stderr-{chunk_index:04d}.log"
                        _write_chunk_queue(chunk_queue, chunk)
                        worker_command = " ".join(
                            shlex.quote(value)
                            for value in worker_values(
                                chunk_queue, cache_dir=work_dir / "musicdl-search-cache"
                            )
                        )
                        prompt = render_prompt(worker_command, chunk_index=chunk_index, chunk_total=len(chunks))
                        prompt_path.write_text(prompt, encoding="utf-8")
                        completed = subprocess.run(
                            claude_command,
                            cwd=workspace,
                            env=env,
                            input=prompt,
                            capture_output=True,
                            text=True,
                            timeout=args.timeout_seconds,
                            check=False,
                        )
                        stdout_path.write_text(completed.stdout, encoding="utf-8")
                        stderr_path.write_text(completed.stderr, encoding="utf-8")
                        chunk_progress = progress_summary(chunk, _load_progress(progress_path))
                        chunk_receipt = {
                            "executor": "claude",
                            "chunk_index": chunk_index,
                            "chunk_count": len(chunk),
                            "queue": str(chunk_queue),
                            "prompt": str(prompt_path),
                            "stdout": str(stdout_path),
                            "stderr": str(stderr_path),
                            "claude_exit_code": completed.returncode,
                            "worker_progress": chunk_progress,
                        }
                        chunk_receipts.append(chunk_receipt)
                        if completed.returncode != 0:
                            raise RuntimeError(f"Claude Code lyric backfill chunk {chunk_index} failed")
                        if chunk_progress["unattempted"]:
                            raise RuntimeError(
                                f"lyric worker did not produce progress for chunk {chunk_index}: "
                                f"{chunk_progress['unattempted']}"
                            )

                worker_progress = compact_progress_summary(
                    progress_summary(rows, _load_progress(progress_path))
                )
                imported_raw, coverage, validation = _import_receipts(database, receipt_path)
                imported = compact_import_summary(imported_raw)
                summary.update(
                    {
                        "chunks": chunk_receipts,
                        "worker_progress": worker_progress,
                        "import": imported,
                        "coverage": coverage,
                        "validation": validation,
                    }
                )
                outputs.update(summary)
                if coverage["unresolved"] and not args.allow_incomplete:
                    raise RuntimeError(
                        "lyric backfill completed its worker attempts but coverage remains unresolved: "
                        f"{coverage}; retry the same run after fixing the pending source responses"
                    )

    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

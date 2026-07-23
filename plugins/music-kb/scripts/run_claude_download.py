#!/usr/bin/env python3
"""Orchestrate inventory -> dedup queue -> Claude Code download execution."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
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
        help="Maximum queue rows per Claude Code session (serial; default: 8)",
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
    parser.add_argument("--proxy", help="例如 http://127.0.0.1:7890；会传给 Claude Code 子进程")
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
    inventory_output = run_checked(build_command, workspace, timeout_seconds=args.timeout_seconds)

    if args.reuse_queue:
        if not queue.is_file() or not manifest.is_file():
            raise RuntimeError("--reuse-queue requires the existing queue and queue_manifest.json")
        queue_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    else:
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
        queue_manifest = json.loads(queue_output)
        manifest.write_text(json.dumps(queue_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rows = load_jsonl_rows(queue)
    if int(queue_manifest.get("queued", len(rows))) != len(rows):
        raise RuntimeError("queue manifest count does not match queue rows")
    progress_before = load_progress(progress)
    inventory_before = json.loads(inventory.read_text(encoding="utf-8"))
    chunks = pending_chunks(rows, progress_before, args.chunk_size, args.max_items, inventory_before, audio_root)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "queue": len(rows),
                    "would_process": sum(len(chunk) for chunk in chunks),
                    "chunk_size": args.chunk_size,
                    "chunks": len(chunks),
                    "reuse_queue": args.reuse_queue,
                    "lyrics_receipt": str(lyrics_receipt),
                    "dry_run": True,
                },
                ensure_ascii=False,
            )
        )
        return 0

    env = os.environ.copy()
    if args.proxy:
        env["https_proxy"] = args.proxy
        env["http_proxy"] = args.proxy
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

    chunk_receipts: list[dict[str, Any]] = []
    last_stdout_path = run_dir / "claude_stdout.json"
    last_stderr_path = run_dir / "claude_stderr.log"
    exit_code = 0
    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_queue = run_dir / f"download-queue-{chunk_index:04d}.jsonl"
        chunk_queue.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in chunk), encoding="utf-8"
        )
        worker_values = [
            "python3",
            str(worker_script),
            "--queue",
            str(chunk_queue),
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
        ]
        worker_command = " ".join(shlex.quote(value) for value in worker_values)
        prompt = render_prompt(worker_command, chunk_index=chunk_index, chunk_total=len(chunks))
        prompt_path = run_dir / f"claude_prompt-{chunk_index:04d}.txt"
        stdout_path = run_dir / f"claude_stdout-{chunk_index:04d}.json"
        stderr_path = run_dir / f"claude_stderr-{chunk_index:04d}.log"
        prompt_path.write_text(prompt, encoding="utf-8")
        result = subprocess.run(
            claude_command,
            cwd=workspace,
            env=env,
            text=True,
            input=prompt,
            capture_output=True,
            timeout=args.timeout_seconds,
        )
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        last_stdout_path.write_text(result.stdout, encoding="utf-8")
        last_stderr_path.write_text(result.stderr, encoding="utf-8")
        progress_after = load_progress(progress)
        inventory_after = json.loads(inventory.read_text(encoding="utf-8"))
        chunk_aggregate = aggregate_progress(chunk, progress_after, inventory_after, audio_root)
        chunk_complete = not chunk_aggregate["unresolved"] and bool(progress_after.get("finished_at"))
        receipt = {
            "chunk_index": chunk_index,
            "chunk_count": len(chunk),
            "queue": str(chunk_queue),
            "prompt": str(prompt_path),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
            "claude_exit_code": result.returncode,
            "complete": chunk_complete and result.returncode == 0,
            "aggregate": chunk_aggregate,
        }
        chunk_receipts.append(receipt)
        if not receipt["complete"]:
            exit_code = 2
            break

    final_progress = load_progress(progress)
    final_inventory = json.loads(inventory.read_text(encoding="utf-8"))
    aggregate = aggregate_progress(rows, final_progress, final_inventory, audio_root)
    if aggregate["unresolved"]:
        exit_code = 2
    summary = {
        "run_id": run_id,
        "inventory_output": json.loads(inventory_output),
        "queue_manifest": queue_manifest,
        "queue": len(rows),
        "chunk_size": args.chunk_size,
        "chunks": chunk_receipts,
        "reuse_queue": args.reuse_queue,
        "run_dir": str(run_dir),
        "claude_stdout": str(last_stdout_path),
        "claude_stderr": str(last_stderr_path),
        "operations_file": str(operations_file),
        "operations_sha256": sha256_file(operations_file),
        "worker_progress": aggregate,
        "worker_progress_incomplete": bool(aggregate["unresolved"]),
        "lyrics_receipt": str(lyrics_receipt),
        "lyrics_receipt_exists": lyrics_receipt.is_file(),
        "claude_exit_code": exit_code,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

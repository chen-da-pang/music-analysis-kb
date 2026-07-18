#!/usr/bin/env python3
"""Orchestrate inventory -> dedup queue -> Claude Code download execution."""

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


def now_run_id(source: Path) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"kugou-download-{stamp}-{source.stem[:32]}"


def run_checked(command: list[str], cwd: Path, *, timeout_seconds: int) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout_seconds)
    if result.returncode:
        raise RuntimeError(f"命令失败 ({result.returncode}): {' '.join(command)}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


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

    worker_values = [
        "python3",
        str(worker_script),
        "--queue",
        str(queue),
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
        "--item-timeout-seconds",
        str(args.item_timeout_seconds),
    ]
    if args.dry_run:
        worker_values.append("--dry-run")
    if args.max_items is not None:
        worker_values.extend(["--max-items", str(args.max_items)])
    worker_command = " ".join(shlex.quote(value) for value in worker_values)
    prompt_lines = [
        "你是本周音乐库下载原子的 Claude Code 执行器。",
        "只执行已经准备好的队列，不要自行改写下载器，不要调用 kugou-cli，也不要重新抓榜单。",
        "历史有效方法是 musicdl 的 MusicClient + KugouMusicClient：按标题和歌手搜索，选择最佳匹配后下载。",
        "严格运行下面这一条命令；只允许读写这些绝对路径。队列为空时不要初始化 musicdl，直接报告没有新增下载。",
        "绝不能手工修改 inventory、progress、queue 或音频状态；只能让固定 worker 写入。不得把未产生文件的歌曲标记为 downloaded、purged_after_analysis 或 skipped。",
        "单曲超时或 musicdl 返回失败时保留真实 failed/no_results 记录并继续队列；不要重写命令、不要手工跳过、不要伪造成功。",
        "必须等待 worker 命令真正退出，并确认 progress.json 有 finished_at 和 summary；不得在命令仍运行时提前返回。",
        worker_command,
        "命令完成后读取 stdout 和 progress.json，只返回 JSON 摘要：run_id、queue、downloaded、skipped_existing、failed、no_results、dry_run。",
        "不要运行旧的 batch_download.py，因为它会从全量 SQLite 重新遍历，不能保证本周队列级去重。",
    ]
    prompt = "\n".join(prompt_lines) + "\n"
    (run_dir / "claude_prompt.txt").write_text(prompt, encoding="utf-8")

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
        "--add-dir",
        str(workspace),
    ]
    if args.model:
        claude_command.extend(["--model", args.model])
    if args.max_budget_usd is not None:
        claude_command.extend(["--max-budget-usd", str(args.max_budget_usd)])
    result = subprocess.run(
        claude_command,
        cwd=workspace,
        env=env,
        text=True,
        input=prompt,
        capture_output=True,
        timeout=args.timeout_seconds,
    )
    (run_dir / "claude_stdout.json").write_text(result.stdout, encoding="utf-8")
    (run_dir / "claude_stderr.log").write_text(result.stderr, encoding="utf-8")
    summary = {
        "run_id": run_id,
        "inventory_output": json.loads(inventory_output),
        "queue_manifest": queue_manifest,
        "claude_exit_code": result.returncode,
        "run_dir": str(run_dir),
        "claude_stdout": str(run_dir / "claude_stdout.json"),
        "claude_stderr": str(run_dir / "claude_stderr.log"),
        "operations_file": str(operations_file),
        "operations_sha256": sha256_file(operations_file),
    }
    if progress.exists():
        try:
            progress_data = json.loads(progress.read_text(encoding="utf-8"))
            worker_summary = progress_data.get("summary")
            summary["worker_progress"] = worker_summary if isinstance(worker_summary, dict) else progress_data
            if queue_manifest.get("queued", 0) and not args.dry_run:
                processed = (
                    int((worker_summary or {}).get("downloaded", 0))
                    + int((worker_summary or {}).get("skipped_existing", 0))
                    + int((worker_summary or {}).get("failed", 0))
                    + int((worker_summary or {}).get("no_results", 0))
                ) if isinstance(worker_summary, dict) else 0
                if not isinstance(worker_summary, dict) or not progress_data.get("finished_at"):
                    summary["worker_progress_incomplete"] = {
                        "reason": "worker exited without finished_at/summary",
                        "results": len(progress_data.get("results", {})),
                    }
                    result.returncode = 2
                elif worker_summary.get("queue") != queue_manifest.get("queued") or processed != queue_manifest.get("queued"):
                    summary["worker_progress_incomplete"] = {
                        "reason": "worker summary does not cover the prepared queue",
                        "worker_summary": worker_summary,
                        "queued": queue_manifest.get("queued"),
                        "processed": processed,
                    }
                    result.returncode = 2
        except (OSError, ValueError) as exc:
            summary["worker_progress_error"] = str(exc)
    elif queue_manifest.get("queued", 0) and result.returncode == 0 and not args.dry_run:
        # A non-empty queue must leave a worker progress file. Otherwise the
        # child reported success without executing the bounded worker.
        summary["worker_progress_missing"] = True
        result.returncode = 2
    summary["claude_exit_code"] = result.returncode
    print(json.dumps(summary, ensure_ascii=False))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

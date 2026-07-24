#!/usr/bin/env python3
"""Prepare the no-results queue and execute the fixed fallback worker.

The direct path defaults to two isolated shards.  Each shard owns a private
queue, inventory copy, progress file, and download staging directory.  Only
after every shard has reached a terminal result does one serial merger move
the verified media and write the real inventory/progress files.  This keeps
the measured two-way download concurrency without allowing concurrent writes
to durable state.

``--executor claude`` remains a bounded, single-worker compatibility retry.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
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


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def queue_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def item_identity(candidate: dict[str, Any]) -> str:
    return str(candidate.get("identity_key") or candidate.get("title_artist_key") or "")


def find_item(inventory: dict[str, Any], candidate: dict[str, Any], *, create: bool) -> dict[str, Any] | None:
    identity = candidate.get("identity_key")
    title_key = candidate.get("title_artist_key")
    for song in inventory.get("songs", []):
        if identity and song.get("identity_key") == identity:
            return song
        if title_key and song.get("title_artist_key") == title_key:
            return song
    if not create:
        return None
    song = copy.deepcopy(candidate)
    inventory.setdefault("songs", []).append(song)
    return song


def downloaded_present(item: dict[str, Any]) -> bool:
    download = item.get("download", {})
    if download.get("status") != "downloaded":
        return False
    if download.get("retention") == "purged_after_analysis":
        return True
    path = download.get("path")
    return bool(path and Path(path).expanduser().is_file())


def refresh_inventory_counts(inventory: dict[str, Any]) -> None:
    inventory["generated_at"] = now_iso()
    inventory["counts"] = {"total": len(inventory.get("songs", []))}
    for song in inventory.get("songs", []):
        status = song.get("download", {}).get("status", "not_attempted")
        inventory["counts"][status] = inventory["counts"].get(status, 0) + 1


def processed_count(summary: dict[str, Any]) -> int:
    return sum(int(summary.get(key, 0)) for key in SUMMARY_KEYS)


def progress_error(progress: dict[str, Any], expected: int, rows: list[dict[str, Any]] | None = None) -> str | None:
    if not progress.get("finished_at"):
        return "progress is missing finished_at"
    actual = processed_count(progress.get("summary", {}))
    if actual != expected:
        return f"progress processed {actual} rows, expected {expected}"
    if rows is not None:
        results = progress.get("results", {})
        for row in rows:
            result = results.get(item_identity(row))
            if not isinstance(result, dict) or result.get("status") not in SUMMARY_KEYS:
                return f"progress is missing a terminal result for {item_identity(row)}"
    return None


def run_checked(command: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {result.stderr[-2000:]}")
    return json.loads(result.stdout)


def worker_values(
    worker_python: str,
    scripts: Path,
    queue: Path,
    inventory: Path,
    work_dir: Path,
    progress: Path,
    run_id: str,
    profile: Path,
    *,
    dry_run: bool,
) -> list[str]:
    values = [
        worker_python,
        str(scripts / "download_music_fallback.py"),
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
    ]
    if dry_run:
        values.append("--dry-run")
    return values


def prepare_isolated_shards(
    *,
    queue: Path,
    inventory: Path,
    run_dir: Path,
    parallelism: int,
    worker_python: str,
    scripts: Path,
    profile: Path,
    run_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = queue_rows(queue)
    shard_root = run_dir / "fallback-shards"
    if shard_root.exists():
        raise RuntimeError(f"refusing to reuse existing fallback shard directory: {shard_root}")
    source_inventory = read_json(inventory)
    inventory_sha256 = sha256_file(inventory)
    shard_root.mkdir(parents=True)
    assignments = [[] for _ in range(parallelism)]
    for index, row in enumerate(rows):
        assignments[index % parallelism].append(row)

    shards: list[dict[str, Any]] = []
    for index, assigned in enumerate(assignments):
        shard_dir = shard_root / f"shard-{index + 1:02d}"
        shard_dir.mkdir(parents=True)
        shard_queue = shard_dir / "queue.jsonl"
        shard_queue.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in assigned), encoding="utf-8")
        shard_songs: list[dict[str, Any]] = []
        for row in assigned:
            existing = find_item(source_inventory, row, create=False)
            shard_songs.append(copy.deepcopy(existing if existing is not None else row))
        shard_inventory = shard_dir / "inventory.json"
        atomic_write_json(
            shard_inventory,
            {
                "schema_version": source_inventory.get("schema_version", 1),
                "generated_at": now_iso(),
                "source_inventory_sha256": inventory_sha256,
                "songs": shard_songs,
            },
        )
        shard_progress = shard_dir / "progress.json"
        shard_work_dir = shard_dir / "music_downloads"
        shard = {
            "index": index + 1,
            "rows": assigned,
            "queue": shard_queue,
            "inventory": shard_inventory,
            "progress": shard_progress,
            "work_dir": shard_work_dir,
            "stdout": shard_dir / "worker_stdout.json",
            "stderr": shard_dir / "worker_stderr.log",
            "run_id": f"{run_id}-shard-{index + 1:02d}",
        }
        shard["command"] = worker_values(
            worker_python,
            scripts,
            shard_queue,
            shard_inventory,
            shard_work_dir,
            shard_progress,
            shard["run_id"],
            profile,
            dry_run=False,
        )
        shards.append(shard)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "parallelism": parallelism,
        "queue_count": len(rows),
        "inventory_sha256_at_start": inventory_sha256,
        "shards": [
            {
                "index": shard["index"],
                "count": len(shard["rows"]),
                "identity_keys": [item_identity(row) for row in shard["rows"]],
                "queue": str(shard["queue"]),
                "inventory": str(shard["inventory"]),
                "progress": str(shard["progress"]),
                "work_dir": str(shard["work_dir"]),
            }
            for shard in shards
        ],
    }
    atomic_write_json(run_dir / "fallback-shard-manifest.json", manifest)
    return shards, manifest


def merged_progress_summary(results: dict[str, dict[str, Any]]) -> dict[str, int]:
    summary = {key: 0 for key in SUMMARY_KEYS}
    for result in results.values():
        status = result.get("status")
        if status in summary:
            summary[status] += 1
    return summary


def merge_isolated_shards(
    *,
    shards: list[dict[str, Any]],
    inventory_path: Path,
    work_dir: Path,
    progress_path: Path,
    run_id: str,
    started_at: str,
    expected_inventory_sha256: str,
) -> dict[str, Any]:
    """Merge terminal shard results in one writer after preflighting every move."""
    if sha256_file(inventory_path) != expected_inventory_sha256:
        raise RuntimeError("real inventory changed while fallback shards were running; refusing to merge")

    shard_data: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for shard in shards:
        if not shard["progress"].is_file():
            raise RuntimeError(f"shard {shard['index']} did not write progress")
        progress = read_json(shard["progress"])
        error = progress_error(progress, len(shard["rows"]), shard["rows"])
        if error:
            raise RuntimeError(f"shard {shard['index']} is incomplete: {error}")
        shard_data.append((shard, read_json(shard["inventory"]), progress))

    real_inventory = read_json(inventory_path)
    combined_results: dict[str, dict[str, Any]] = {}
    planned_moves: dict[Path, Path] = {}
    updates: list[tuple[str, dict[str, Any], dict[str, Any], Path]] = []
    preserved_existing = 0

    for shard, shard_inventory, shard_progress in shard_data:
        for candidate in shard["rows"]:
            identity = item_identity(candidate)
            result = copy.deepcopy(shard_progress.get("results", {}).get(identity, {}))
            result["shard"] = shard["index"]
            combined_results[identity] = result
            shard_item = find_item(shard_inventory, candidate, create=False)
            if shard_item is None:
                raise RuntimeError(f"shard {shard['index']} lost inventory item {identity}")
            real_item = find_item(real_inventory, candidate, create=True)
            assert real_item is not None
            shard_download = copy.deepcopy(shard_item.get("download", {}))
            shard_status = shard_download.get("status")
            if downloaded_present(real_item):
                preserved_existing += 1
                result["merge"] = "preserved_existing_download"
                continue
            if shard_status != "downloaded":
                real_item["download"] = shard_download
                continue

            raw_path = shard_download.get("path")
            source_path = Path(raw_path).expanduser().resolve() if raw_path else None
            if source_path is None or not source_path.is_file():
                raise RuntimeError(f"shard {shard['index']} marked {identity} downloaded without a file")
            try:
                relative_path = source_path.relative_to(shard["work_dir"].resolve())
            except ValueError as exc:
                raise RuntimeError(f"shard {shard['index']} media path escaped staging: {source_path}") from exc
            source_directory = source_path.parent
            target_path = work_dir / relative_path
            target_directory = target_path.parent
            if source_directory.parent == shard["work_dir"].resolve():
                raise RuntimeError(f"shard {shard['index']} did not create a per-song media directory")
            if target_path.exists() or target_directory.exists():
                raise RuntimeError(f"refusing to overwrite existing fallback media directory: {target_directory}")
            previous = planned_moves.get(source_directory)
            if previous is not None and previous != target_directory:
                raise RuntimeError(f"one staging directory maps to multiple targets: {source_directory}")
            planned_moves[source_directory] = target_directory
            updates.append((identity, real_item, shard_download, target_path))

    for source_directory, target_directory in planned_moves.items():
        target_directory.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_directory), str(target_directory))

    for identity, real_item, shard_download, target_path in updates:
        assert target_path.is_file()
        shard_download["path"] = str(target_path.resolve())
        shard_download["file_present"] = True
        shard_download["exists"] = True
        real_item["download"] = shard_download
        combined_results[identity]["path"] = str(target_path.resolve())

    refresh_inventory_counts(real_inventory)
    atomic_write_json(inventory_path, real_inventory)
    progress = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": now_iso(),
        "parallelism": len(shards),
        "results": combined_results,
        "summary": merged_progress_summary(combined_results),
        "merge": {
            "media_directories_moved": len(planned_moves),
            "preserved_existing_downloads": preserved_existing,
        },
    }
    atomic_write_json(progress_path, progress)
    return progress


def execute_isolated_parallel(
    *,
    args: argparse.Namespace,
    scripts: Path,
    workspace: Path,
    run_dir: Path,
    queue: Path,
    inventory: Path,
    work_dir: Path,
    progress: Path,
    profile: Path,
    env: dict[str, str],
    started_at: str,
) -> tuple[int, dict[str, Any]]:
    shards, manifest = prepare_isolated_shards(
        queue=queue,
        inventory=inventory,
        run_dir=run_dir,
        parallelism=args.parallelism,
        worker_python=args.worker_python,
        scripts=scripts,
        profile=profile,
        run_id=args.run_id,
    )
    processes: list[tuple[dict[str, Any], subprocess.Popen[str]]] = []
    for shard in shards:
        process = subprocess.Popen(
            shard["command"],
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append((shard, process))

    failures: list[dict[str, Any]] = []
    for shard, process in processes:
        try:
            stdout, stderr = process.communicate(timeout=args.timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            failures.append({"shard": shard["index"], "error": f"timed out after {args.timeout_seconds}s"})
        shard["stdout"].write_text(stdout, encoding="utf-8")
        shard["stderr"].write_text(stderr, encoding="utf-8")
        shard["returncode"] = process.returncode
        if process.returncode:
            failures.append({"shard": shard["index"], "error": f"worker exited {process.returncode}"})
        if shard["progress"].is_file():
            shard["progress_data"] = read_json(shard["progress"])
            error = progress_error(shard["progress_data"], len(shard["rows"]), shard["rows"])
            if error:
                failures.append({"shard": shard["index"], "error": error})
        else:
            failures.append({"shard": shard["index"], "error": "progress missing"})

    state: dict[str, Any] = {
        "parallelism": args.parallelism,
        "shard_manifest": str(run_dir / "fallback-shard-manifest.json"),
        "commands": [shard["command"] for shard in shards],
        "shards": [
            {
                "index": shard["index"],
                "queue": str(shard["queue"]),
                "inventory": str(shard["inventory"]),
                "progress": str(shard["progress"]),
                "work_dir": str(shard["work_dir"]),
                "stdout": str(shard["stdout"]),
                "stderr": str(shard["stderr"]),
                "returncode": shard.get("returncode"),
            }
            for shard in shards
        ],
    }
    if failures:
        state["merge"] = "not_run"
        state["failures"] = failures
        return 2, state
    try:
        merged = merge_isolated_shards(
            shards=shards,
            inventory_path=inventory,
            work_dir=work_dir,
            progress_path=progress,
            run_id=args.run_id,
            started_at=started_at,
            expected_inventory_sha256=manifest["inventory_sha256_at_start"],
        )
    except Exception as exc:
        state["merge"] = "not_run"
        state["failures"] = [{"merge": f"{type(exc).__name__}: {exc}"}]
        return 2, state
    state["merge"] = merged.get("merge", {})
    state["merged_summary"] = merged.get("summary", {})
    return 0, state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument(
        "--executor",
        choices=("direct", "claude"),
        default="direct",
        help="direct runs fixed workers (default); claude preserves the legacy executor",
    )
    parser.add_argument(
        "--parallelism",
        type=int,
        choices=(1, 2),
        default=2,
        help="number of isolated direct fallback shards; two is the measured maximum (default: 2)",
    )
    parser.add_argument(
        "--worker-python",
        default=os.environ.get("MUSICDL_PYTHON", "python3"),
        help="Python executable with musicdl for the fallback worker",
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--proxy", help="for example http://127.0.0.1:7890; passed to every direct shard")
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
    work_dir = workspace / "music_downloads" / "KugouMusicClient"
    scripts = Path(__file__).resolve().parent
    manifest = run_checked(
        [sys.executable, str(scripts / "prepare_fallback_queue.py"), "--inventory", str(inventory), "--output", str(queue), "--profile", str(profile)],
        workspace,
        args.timeout_seconds,
    )
    atomic_write_json(run_dir / "fallback-queue-manifest.json", manifest)
    values = worker_values(args.worker_python, scripts, queue, inventory, work_dir, progress, args.run_id, profile, dry_run=args.dry_run)
    env = os.environ.copy()
    if args.proxy:
        env["http_proxy"] = args.proxy
        env["https_proxy"] = args.proxy

    worker_exit_code = 0
    claude_exit_code: int | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    parallel_state: dict[str, Any] | None = None
    effective_parallelism = 1
    if manifest["queued"] == 0 and not args.dry_run:
        execution = "skipped_empty_queue"
    elif args.executor == "direct" and not args.dry_run and args.parallelism > 1 and manifest["queued"] > 1:
        execution = "direct_parallel"
        effective_parallelism = args.parallelism
        worker_exit_code, parallel_state = execute_isolated_parallel(
            args=args,
            scripts=scripts,
            workspace=workspace,
            run_dir=run_dir,
            queue=queue,
            inventory=inventory,
            work_dir=work_dir,
            progress=progress,
            profile=profile,
            env=env,
            started_at=started_at,
        )
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

    summary: dict[str, Any] = {
        "run_id": args.run_id,
        "queue_manifest": manifest,
        "executor": args.executor,
        "execution": execution,
        "requested_parallelism": args.parallelism,
        "parallelism": effective_parallelism,
        "worker_python": args.worker_python,
        "worker_exit_code": worker_exit_code,
        "claude_exit_code": claude_exit_code,
        "stdout": str(stdout_path) if stdout_path is not None else None,
        "stderr": str(stderr_path) if stderr_path is not None else None,
        "progress": str(progress),
        "operations_sha256": sha256_file(operations),
        "profile": str(profile),
    }
    if parallel_state is not None:
        summary["parallel"] = parallel_state
    if progress.exists():
        progress_data = read_json(progress)
        summary["worker_progress"] = progress_data
        error = progress_error(progress_data, int(manifest["queued"]))
        if not args.dry_run and error:
            summary["progress_incomplete"] = {"queued": manifest["queued"], "error": error}
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
        "parallel_commands": parallel_state.get("commands") if parallel_state else None,
    }
    receipt_path = workspace / "data" / "weekly_runs" / args.run_id / "atoms" / "fallback_download.json"
    atomic_write_json(receipt_path, receipt)
    summary["receipt"] = str(receipt_path)
    print(json.dumps(summary, ensure_ascii=False))
    return worker_exit_code


if __name__ == "__main__":
    raise SystemExit(main())

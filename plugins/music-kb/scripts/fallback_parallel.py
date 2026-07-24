#!/usr/bin/env python3
"""Run state-isolated fallback shards and merge their terminal results.

The direct path defaults to two isolated shards.  Each shard owns a private
queue, inventory copy, progress file, and download staging directory.  Only
after every shard has reached a terminal result does one serial merger move
the verified media and write the real inventory/progress files.  This keeps
the measured two-way download concurrency without allowing concurrent writes
to durable state.

This module is an internal controller used by the detached fallback supervisor.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from music_kb.operation_context import sha256_file


SUMMARY_KEYS = ("downloaded", "skipped_existing", "failed", "no_results", "abandoned")


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
    planned_targets: dict[Path, Path] = {}
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
            previous_source = planned_targets.get(target_directory)
            if previous_source is not None and previous_source != source_directory:
                raise RuntimeError(f"two staging directories map to one target: {target_directory}")
            planned_moves[source_directory] = target_directory
            planned_targets[target_directory] = source_directory
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
    args: Any,
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
    deadline = time.monotonic() + args.timeout_seconds
    for shard, process in processes:
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise subprocess.TimeoutExpired(shard["command"], args.timeout_seconds)
            stdout, stderr = process.communicate(timeout=remaining)
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
        "timeout_seconds": args.timeout_seconds,
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

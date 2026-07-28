#!/usr/bin/env python3
"""Run primary Kugou workers in isolated shards and merge them safely.

The primary worker is deliberately kept unchanged: it still owns one
``MusicClient`` and writes its own inventory/progress/lyric receipt.  This
controller only gives each worker a private copy of those files and a private
staging directory.  The formal inventory, progress file, audio tree, and
lyrics receipt are written by one serial merger after every shard is terminal.

It is used by ``launch_music_primary_worker.py``.  Claude Code remains the
outer executor; this module is not a replacement for Claude and must not be
invoked by the weekly orchestrator directly in the normal path.
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
from typing import Any, Mapping

from music_kb.operation_context import sha256_file


SUMMARY_KEYS = ("downloaded", "skipped_existing", "failed", "no_results")
TERMINAL_STATUSES = set(SUMMARY_KEYS)


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


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON receipt must be an object: {path}")
    return value


def queue_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError(f"queue row must be an object: {path}")
        rows.append(value)
    return rows


def item_identity(candidate: Mapping[str, Any]) -> str:
    identity = str(candidate.get("identity_key") or candidate.get("title_artist_key") or "").strip()
    if not identity:
        raise RuntimeError("queue row has no stable identity key")
    return identity


def find_item(
    inventory: dict[str, Any], candidate: Mapping[str, Any], *, create: bool
) -> dict[str, Any] | None:
    identity = str(candidate.get("identity_key") or "").strip()
    title_key = str(candidate.get("title_artist_key") or "").strip()
    for song in inventory.get("songs", []):
        if not isinstance(song, dict):
            continue
        if identity and song.get("identity_key") == identity:
            return song
        if title_key and song.get("title_artist_key") == title_key:
            return song
    if not create:
        return None
    song = copy.deepcopy(dict(candidate))
    inventory.setdefault("songs", []).append(song)
    return song


def refresh_inventory_counts(inventory: dict[str, Any]) -> None:
    inventory["generated_at"] = now_iso()
    counts: dict[str, int] = {"total": len(inventory.get("songs", []))}
    for song in inventory.get("songs", []):
        if not isinstance(song, dict):
            continue
        status = str((song.get("download") or {}).get("status", "not_attempted"))
        counts[status] = counts.get(status, 0) + 1
    inventory["counts"] = counts


def downloaded_present(item: Mapping[str, Any]) -> bool:
    download = item.get("download")
    if not isinstance(download, Mapping) or download.get("status") != "downloaded":
        return False
    if download.get("retention") == "purged_after_analysis":
        return True
    raw_path = download.get("path")
    return bool(raw_path and Path(str(raw_path)).expanduser().is_file())


def downloaded_present_in_root(item: Mapping[str, Any], audio_root: Path) -> bool:
    """Check a formal inventory path, which may be relative to audio_root."""

    download = item.get("download")
    if not isinstance(download, Mapping) or download.get("status") != "downloaded":
        return False
    if download.get("retention") == "purged_after_analysis":
        return True
    raw_path = download.get("path")
    if not raw_path:
        return False
    path = Path(str(raw_path)).expanduser()
    if not path.is_absolute():
        path = audio_root / path
    return path.is_file()


def processed_count(progress: Mapping[str, Any]) -> int:
    summary = progress.get("summary")
    if not isinstance(summary, Mapping):
        return 0
    return sum(int(summary.get(key, 0) or 0) for key in SUMMARY_KEYS)


def progress_error(
    progress: Mapping[str, Any], expected: int, rows: list[Mapping[str, Any]] | None = None
) -> str | None:
    if not progress.get("finished_at"):
        return "progress is missing finished_at"
    actual = processed_count(progress)
    if actual != expected:
        return f"progress processed {actual} rows, expected {expected}"
    if rows is not None:
        results = progress.get("results")
        if not isinstance(results, Mapping):
            return "progress is missing results"
        for row in rows:
            identity = item_identity(row)
            result = results.get(identity)
            if not isinstance(result, Mapping) or result.get("status") not in TERMINAL_STATUSES:
                return f"progress is missing a terminal result for {identity}"
    return None


def worker_values(
    *,
    worker_python: str,
    scripts: Path,
    queue: Path,
    inventory: Path,
    work_dir: Path,
    progress: Path,
    log: Path,
    lyrics_receipt: Path,
    run_id: str,
    item_timeout_seconds: float,
    lookup_mode: str,
    worker_delay: float,
) -> list[str]:
    return [
        worker_python,
        str(scripts / "download_music_queue.py"),
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
        "--lyrics-receipt",
        str(lyrics_receipt),
        "--item-timeout-seconds",
        str(item_timeout_seconds),
        "--lookup-mode",
        lookup_mode,
        "--delay",
        str(worker_delay),
    ]


def prepare_isolated_shards(
    *,
    queue: Path,
    inventory: Path,
    run_dir: Path,
    parallelism: int,
    worker_python: str,
    scripts: Path,
    run_id: str,
    item_timeout_seconds: float,
    lookup_mode: str,
    worker_delay: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if parallelism not in (1, 2):
        raise ValueError("primary parallelism must be 1 or 2")
    rows = queue_rows(queue)
    # Do not start an empty musicdl client merely to fill a second shard for
    # a one-song smoke run.  The requested upper bound is still two workers.
    parallelism = min(parallelism, max(1, len(rows)))
    shard_root = run_dir / "primary-shards"
    if shard_root.exists():
        raise RuntimeError(f"refusing to reuse existing primary shard directory: {shard_root}")
    source_inventory = read_json(inventory)
    inventory_sha256 = sha256_file(inventory)
    shard_root.mkdir(parents=True)
    assignments: list[list[dict[str, Any]]] = [[] for _ in range(parallelism)]
    for index, row in enumerate(rows):
        assignments[index % parallelism].append(row)

    shards: list[dict[str, Any]] = []
    for index, assigned in enumerate(assignments):
        shard_dir = shard_root / f"shard-{index + 1:02d}"
        shard_dir.mkdir(parents=True)
        shard_queue = shard_dir / "queue.jsonl"
        shard_queue.write_text(
            "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in assigned),
            encoding="utf-8",
        )
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
        shard_log = shard_dir / "download.log"
        shard_lyrics = shard_dir / "lyrics-receipts.jsonl"
        shard: dict[str, Any] = {
            "index": index + 1,
            "rows": assigned,
            "queue": shard_queue,
            "inventory": shard_inventory,
            "progress": shard_progress,
            "work_dir": shard_work_dir,
            "log": shard_log,
            "lyrics_receipt": shard_lyrics,
            "stdout": shard_dir / "worker_stdout.json",
            "stderr": shard_dir / "worker_stderr.log",
            "run_id": f"{run_id}-shard-{index + 1:02d}",
        }
        shard["command"] = worker_values(
            worker_python=worker_python,
            scripts=scripts,
            queue=shard_queue,
            inventory=shard_inventory,
            work_dir=shard_work_dir,
            progress=shard_progress,
            log=shard_log,
            lyrics_receipt=shard_lyrics,
            run_id=shard["run_id"],
            item_timeout_seconds=item_timeout_seconds,
            lookup_mode=lookup_mode,
            worker_delay=worker_delay,
        )
        shards.append(shard)

    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "parallelism": parallelism,
        "queue_count": len(rows),
        "inventory_sha256_at_start": inventory_sha256,
        "lookup_mode": lookup_mode,
        "item_timeout_seconds": item_timeout_seconds,
        "worker_delay": worker_delay,
        "shards": [
            {
                "index": shard["index"],
                "count": len(shard["rows"]),
                "identity_keys": [item_identity(row) for row in shard["rows"]],
                "queue": str(shard["queue"]),
                "inventory": str(shard["inventory"]),
                "progress": str(shard["progress"]),
                "work_dir": str(shard["work_dir"]),
                "lyrics_receipt": str(shard["lyrics_receipt"]),
            }
            for shard in shards
        ],
    }
    atomic_write_json(run_dir / "primary-shard-manifest.json", manifest)
    return shards, manifest


def merged_progress_summary(results: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    summary = {key: 0 for key in SUMMARY_KEYS}
    for result in results.values():
        status = result.get("status")
        if status in summary:
            summary[str(status)] += 1
    return summary


def _receipt_identity(receipt: Mapping[str, Any]) -> str:
    return str(receipt.get("identity_key") or receipt.get("source_track_id") or "").strip()


def _load_receipt_lines(path: Path, expected: set[str]) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError(f"lyrics receipt line is not an object: {path}")
        identity = _receipt_identity(value)
        # Older receipts use source_track_id (kugou-<id>) while queue rows use
        # identity_key (kugou:<id>). Accept either exact row identity or the
        # corresponding source-track spelling, but reject unrelated records.
        allowed = identity in expected or any(
            identity == str(row_identity).replace("kugou:", "kugou-", 1)
            for row_identity in expected
            if row_identity.startswith("kugou:")
        )
        if expected and identity and not allowed:
            raise RuntimeError(f"lyrics receipt identity escaped shard: {identity} ({path})")
        records.append(value)
    return records


def _append_merged_receipts(
    *,
    shards: list[dict[str, Any]],
    destination: Path,
    expected_rows: list[Mapping[str, Any]],
) -> int:
    expected = {item_identity(row) for row in expected_rows}
    existing: list[dict[str, Any]] = []
    if destination.is_file():
        existing = _load_receipt_lines(destination, set())
    seen = {_receipt_identity(item) for item in existing if _receipt_identity(item)}
    appended = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as handle:
        for shard in shards:
            for record in _load_receipt_lines(shard["lyrics_receipt"], expected):
                identity = _receipt_identity(record)
                if identity and identity in seen:
                    continue
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                if identity:
                    seen.add(identity)
                appended += 1
    return appended


def merge_isolated_shards(
    *,
    shards: list[dict[str, Any]],
    inventory_path: Path,
    work_dir: Path,
    progress_path: Path,
    lyrics_receipt_path: Path,
    run_id: str,
    started_at: str,
    expected_inventory_sha256: str,
    queue_path: Path,
) -> dict[str, Any]:
    """Merge terminal shard results in one writer after all preflight checks."""

    if sha256_file(inventory_path) != expected_inventory_sha256:
        raise RuntimeError("real inventory changed while primary shards were running; refusing to merge")

    shard_data: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for shard in shards:
        if not shard["progress"].is_file():
            raise RuntimeError(f"primary shard {shard['index']} did not write progress")
        progress = read_json(shard["progress"])
        error = progress_error(progress, len(shard["rows"]), shard["rows"])
        if error:
            raise RuntimeError(f"primary shard {shard['index']} is incomplete: {error}")
        shard_inventory = read_json(shard["inventory"])
        if shard_inventory.get("source_inventory_sha256") != expected_inventory_sha256:
            raise RuntimeError(f"primary shard {shard['index']} inventory provenance changed")
        shard_data.append((shard, shard_inventory, progress))

    real_inventory = read_json(inventory_path)
    combined_results: dict[str, dict[str, Any]] = {}
    combined_downloaded: dict[str, Any] = {}
    combined_lyrics: dict[str, Any] = {}
    combined_timings: dict[str, Any] = {}
    planned_moves: dict[Path, Path] = {}
    planned_targets: dict[Path, Path] = {}
    updates: list[tuple[str, dict[str, Any], dict[str, Any], Path]] = []
    preserved_existing = 0

    for shard, shard_inventory, shard_progress in shard_data:
        shard_results = shard_progress.get("results")
        if not isinstance(shard_results, Mapping):
            raise RuntimeError(f"primary shard {shard['index']} progress has no results")
        for candidate in shard["rows"]:
            identity = item_identity(candidate)
            result = copy.deepcopy(shard_results.get(identity, {}))
            if not isinstance(result, dict):
                raise RuntimeError(f"primary shard {shard['index']} result is not an object: {identity}")
            result["shard"] = shard["index"]
            combined_results[identity] = result
            shard_item = find_item(shard_inventory, candidate, create=False)
            if shard_item is None:
                raise RuntimeError(f"primary shard {shard['index']} lost inventory item {identity}")
            real_item = find_item(real_inventory, candidate, create=True)
            assert real_item is not None
            shard_download = copy.deepcopy(shard_item.get("download") or {})
            if downloaded_present_in_root(real_item, work_dir / "KugouMusicClient"):
                preserved_existing += 1
                result["merge"] = "preserved_existing_download"
                continue
            if shard_download.get("status") != "downloaded":
                real_item["download"] = shard_download
                continue

            raw_path = shard_download.get("path")
            source_path = Path(str(raw_path)).expanduser() if raw_path else None
            if source_path is not None and not source_path.is_absolute():
                # download_music_queue records paths relative to its
                # ``KugouMusicClient`` audio root.
                source_path = shard["work_dir"] / "KugouMusicClient" / source_path
            source_path = source_path.resolve() if source_path is not None else None
            if source_path is None or not source_path.is_file():
                raise RuntimeError(f"primary shard {shard['index']} marked {identity} downloaded without a file")
            try:
                relative_path = source_path.relative_to(shard["work_dir"].resolve())
            except ValueError as exc:
                raise RuntimeError(f"primary shard {shard['index']} media path escaped staging: {source_path}") from exc
            source_directory = source_path.parent
            target_path = work_dir / relative_path
            target_directory = target_path.parent
            if source_directory.parent == shard["work_dir"].resolve():
                raise RuntimeError(f"primary shard {shard['index']} did not create a per-song media directory")
            if target_path.exists() or target_directory.exists():
                raise RuntimeError(f"refusing to overwrite existing primary media directory: {target_directory}")
            previous = planned_moves.get(source_directory)
            if previous is not None and previous != target_directory:
                raise RuntimeError(f"one staging directory maps to multiple targets: {source_directory}")
            previous_source = planned_targets.get(target_directory)
            if previous_source is not None and previous_source != source_directory:
                raise RuntimeError(f"two staging directories map to one target: {target_directory}")
            planned_moves[source_directory] = target_directory
            planned_targets[target_directory] = source_directory
            updates.append((identity, real_item, shard_download, target_path))

        shard_downloaded = shard_progress.get("downloaded")
        if isinstance(shard_downloaded, Mapping):
            combined_downloaded.update(copy.deepcopy(dict(shard_downloaded)))
        shard_lyrics = shard_progress.get("lyrics")
        if isinstance(shard_lyrics, Mapping):
            combined_lyrics.update(copy.deepcopy(dict(shard_lyrics)))
        shard_timings = shard_progress.get("item_timings")
        if isinstance(shard_timings, Mapping):
            combined_timings.update(copy.deepcopy(dict(shard_timings)))

    # All checks above happen before the first move/write, so a failed shard or
    # path collision leaves the formal state untouched.
    for source_directory, target_directory in planned_moves.items():
        target_directory.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_directory), str(target_directory))

    for identity, real_item, shard_download, target_path in updates:
        if not target_path.is_file():
            raise RuntimeError(f"merged media disappeared before state commit: {target_path}")
        shard_download["path"] = str(target_path.resolve())
        shard_download["file_present"] = True
        shard_download["exists"] = True
        real_item["download"] = shard_download
        combined_results[identity]["path"] = str(target_path.resolve())
        if identity in combined_downloaded and isinstance(combined_downloaded[identity], dict):
            combined_downloaded[identity]["file"] = str(target_path.resolve())

    receipt_count = _append_merged_receipts(
        shards=shards,
        destination=lyrics_receipt_path,
        expected_rows=[row for shard in shards for row in shard["rows"]],
    )
    refresh_inventory_counts(real_inventory)
    atomic_write_json(inventory_path, real_inventory)
    progress = {
        "schema_version": 2,
        "run_id": run_id,
        "queue": str(queue_path.resolve()),
        "started_at": started_at,
        "finished_at": now_iso(),
        "parallelism": len(shards),
        "results": combined_results,
        "downloaded": combined_downloaded,
        "lyrics": combined_lyrics,
        "item_timings": combined_timings,
        "summary": merged_progress_summary(combined_results),
        "merge": {
            "media_directories_moved": len(planned_moves),
            "preserved_existing_downloads": preserved_existing,
            "lyrics_receipts_appended": receipt_count,
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
    lyrics_receipt: Path,
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
        run_id=args.run_id,
        item_timeout_seconds=args.item_timeout_seconds,
        lookup_mode=args.lookup_mode,
        worker_delay=args.worker_delay,
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
        shard["stdout"].write_text(stdout or "", encoding="utf-8")
        shard["stderr"].write_text(stderr or "", encoding="utf-8")
        shard["returncode"] = process.returncode
        if process.returncode:
            failures.append({"shard": shard["index"], "error": f"worker exited {process.returncode}"})
        if shard["progress"].is_file():
            try:
                shard["progress_data"] = read_json(shard["progress"])
                error = progress_error(shard["progress_data"], len(shard["rows"]), shard["rows"])
                if error:
                    failures.append({"shard": shard["index"], "error": error})
            except Exception as exc:
                failures.append({"shard": shard["index"], "error": f"invalid progress: {exc}"})
        else:
            failures.append({"shard": shard["index"], "error": "progress missing"})

    state: dict[str, Any] = {
        "parallelism": args.parallelism,
        "timeout_seconds": args.timeout_seconds,
        "shard_manifest": str(run_dir / "primary-shard-manifest.json"),
        "commands": [shard["command"] for shard in shards],
        "shards": [
            {
                "index": shard["index"],
                "queue": str(shard["queue"]),
                "inventory": str(shard["inventory"]),
                "progress": str(shard["progress"]),
                "work_dir": str(shard["work_dir"]),
                "lyrics_receipt": str(shard["lyrics_receipt"]),
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
            lyrics_receipt_path=lyrics_receipt,
            run_id=args.run_id,
            started_at=started_at,
            expected_inventory_sha256=manifest["inventory_sha256_at_start"],
            queue_path=queue,
        )
    except Exception as exc:
        state["merge"] = "not_run"
        state["failures"] = [{"merge": f"{type(exc).__name__}: {exc}"}]
        return 2, state
    state["merge"] = merged.get("merge", {})
    state["merged_summary"] = merged.get("summary", {})
    return 0, state

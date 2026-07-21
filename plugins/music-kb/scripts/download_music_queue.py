#!/usr/bin/env python3
"""Download only the songs in a prepared queue with musicdl.

This script is intentionally deterministic and is normally *run by Claude
Code*, not called directly by the publisher skill.  It updates the inventory
after every attempt so an interrupted run can be resumed without downloading
an already-present song again.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MATCH_POLICY = "exact_title_compatible_artist_v1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ItemTimeoutError(RuntimeError):
    """Raised when one musicdl operation does not return in time."""


@contextmanager
def item_timeout(seconds: float):
    """Bound one musicdl network operation without changing its API."""

    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def raise_timeout(_signum: int, _frame: Any) -> None:
        raise ItemTimeoutError(f"musicdl operation timed out after {seconds:g}s")

    previous_handler = signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def normalize_title(value: Any) -> str:
    """Normalize only presentation-equivalent title differences.

    NFKC handles full-width punctuation. Removing whitespace immediately
    around brackets accepts harmless API formatting differences without
    collapsing meaningful words or version qualifiers.
    """

    return re.sub(r"\s*([()\[\]{}])\s*", r"\1", normalize(value))


def split_artists(value: Any) -> set[str]:
    text = normalize(value)
    if not text or text == "null":
        return set()
    return {
        normalize(part)
        for part in re.split(r"\s*(?:、|&|/|,|，|;|；)\s*", text)
        if normalize(part)
    }


def artists_match(target: Any, found: Any) -> bool:
    wanted = normalize(target)
    candidate = normalize(found)
    if not wanted or not candidate:
        return False
    if wanted == candidate:
        return wanted != "null"
    wanted_artists = split_artists(wanted)
    candidate_artists = split_artists(candidate)
    return bool(wanted_artists and candidate_artists) and wanted_artists == candidate_artists


def title_artist_key(platform: str, title: Any, artist: Any) -> str:
    return f"{platform}:{normalize(title)}\x00{normalize(artist)}"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_queue(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def path_for_download(raw_path: Any, audio_root: Path) -> Path:
    path = Path(str(raw_path)).expanduser()
    return path if path.is_absolute() else audio_root / path


def present(song: dict[str, Any], audio_root: Path) -> bool:
    download = song.get("download")
    if not isinstance(download, dict) or download.get("status") != "downloaded":
        return False
    if download.get("retention") == "purged_after_analysis":
        return True
    raw_path = download.get("path")
    return bool(raw_path and path_for_download(raw_path, audio_root).is_file())


def relative_path(path: Path, audio_root: Path) -> str:
    try:
        return path.resolve().relative_to(audio_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def file_record(path: Path, audio_root: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "status": "downloaded",
        "retention": "retained",
        "path": relative_path(path, audio_root),
        "extension": path.suffix.lower().lstrip("."),
        "exists": True,
        "file_present": True,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": None,
        "recorded_at": now_iso(),
    }


def choose_match(results: list[Any], title: str, artist: str) -> Any | None:
    """Return only a result that preserves the queue item's song identity."""

    wanted_title = normalize_title(title)
    for item in results:
        item_title = normalize_title(getattr(item, "song_name", ""))
        if item_title == wanted_title and artists_match(artist, getattr(item, "singers", "")):
            return item
    return None


def result_metadata(item: Any) -> dict[str, Any]:
    return {
        "source": str(getattr(item, "source", None) or "KugouMusicClient"),
        "matched_title": str(getattr(item, "song_name", "") or ""),
        "matched_artist": str(getattr(item, "singers", "") or ""),
        "match_policy": MATCH_POLICY,
    }


def rejected_candidate_metadata(results: list[Any], limit: int = 10) -> list[dict[str, str]]:
    return [
        {
            "title": str(getattr(item, "song_name", "") or ""),
            "artist": str(getattr(item, "singers", "") or ""),
        }
        for item in results[:limit]
    ]


def update_inventory_song(inventory: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    songs = inventory.setdefault("songs", [])
    identity = candidate.get("identity_key")
    title_key = candidate.get("title_artist_key") or title_artist_key(
        candidate.get("platform", "kugou"), candidate.get("title"), candidate.get("artist")
    )
    item = next((song for song in songs if song.get("identity_key") == identity), None) if identity else None
    if item is None:
        item = next((song for song in songs if song.get("title_artist_key") == title_key), None)
    if item is None:
        item = dict(candidate)
        item.setdefault("source_runs", [])
        item.setdefault("chart_appearances", [])
        songs.append(item)
    else:
        for key in ("identity_key", "title_artist_key", "platform", "platform_track_key", "title", "artist", "play_link"):
            if candidate.get(key) is not None:
                item[key] = candidate[key]
        if candidate.get("source_run") and candidate["source_run"] not in item.setdefault("source_runs", []):
            item["source_runs"].append(candidate["source_run"])
        if candidate.get("chart_appearances"):
            item["chart_appearances"] = candidate["chart_appearances"]
    item["last_seen_at"] = now_iso()
    return item


def record_attempt(item: dict[str, Any], status: str, **extra: Any) -> None:
    item["download"] = {
        "status": status,
        "retention": "retained",
        "path": None,
        "file_present": False,
        "exists": False,
        "size_bytes": None,
        "mtime_ns": None,
        "sha256": None,
        "recorded_at": now_iso(),
        **extra,
    }


def run_download(
    queue_path: Path,
    inventory_path: Path,
    work_dir: Path,
    progress_path: Path,
    log_path: Path,
    run_id: str,
    max_items: int | None,
    dry_run: bool,
    delay: float,
    retries: int,
    search_size: int,
    item_timeout_seconds: float,
) -> dict[str, Any]:
    if item_timeout_seconds <= 0:
        raise ValueError("item-timeout-seconds must be positive")
    queue = load_queue(queue_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    audio_root = work_dir / "KugouMusicClient"
    audio_root.mkdir(parents=True, exist_ok=True)
    progress = {
        "schema_version": 1,
        "run_id": run_id,
        "queue": str(queue_path.resolve()),
        "started_at": now_iso(),
        "downloaded": {},
        "results": {},
    }
    if progress_path.exists():
        try:
            old = json.loads(progress_path.read_text(encoding="utf-8"))
            if old.get("run_id") == run_id:
                progress = old
        except (ValueError, OSError):
            pass
    # A chunk/session retry must not inherit the previous chunk's terminal
    # marker. The wrapper uses this to distinguish a complete worker exit from
    # a Claude session that was cut off mid-command.
    progress.pop("finished_at", None)
    progress.pop("summary", None)

    summary = {"run_id": run_id, "queue": len(queue), "downloaded": 0, "skipped_existing": 0, "failed": 0, "no_results": 0, "dry_run": dry_run}
    selected = queue if max_items is None else queue[:max_items]
    if dry_run:
        summary["would_process"] = len(selected)
        summary["remaining_after_limit"] = max(0, len(queue) - len(selected))
        print(json.dumps(summary, ensure_ascii=False))
        return summary

    try:
        from musicdl.musicdl import MusicClient
    except Exception as exc:  # pragma: no cover - environment-dependent
        raise SystemExit(f"无法导入 musicdl，请在 Claude Code 环境安装 musicdl: {exc}") from exc

    client = MusicClient(
        music_sources=["KugouMusicClient"],
        init_music_clients_cfg={
            "KugouMusicClient": {
                "work_dir": str(work_dir),
                "search_size_per_source": search_size,
            }
        },
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] {message}\n")

    by_identity = {song.get("identity_key"): song for song in inventory.get("songs", [])}
    by_title = {song.get("title_artist_key"): song for song in inventory.get("songs", [])}
    for index, candidate in enumerate(selected, start=1):
        identity = candidate.get("identity_key") or candidate.get("title_artist_key")
        item = by_identity.get(candidate.get("identity_key")) or by_title.get(candidate.get("title_artist_key"))
        if item and present(item, audio_root):
            summary["skipped_existing"] += 1
            progress["results"][identity] = {"status": "skipped_existing", "at": now_iso()}
            continue

        title, artist = candidate["title"], candidate["artist"]
        item = update_inventory_song(inventory, candidate)
        log(f"[{index}/{len(selected)}] {title} - {artist}")
        found: list[Any] | None = None
        last_error = None
        for attempt in range(retries + 1):
            try:
                with item_timeout(item_timeout_seconds):
                    found = client.search(f"{title} {artist}").get("KugouMusicClient", [])
                break
            except ItemTimeoutError as exc:
                # A timeout is a bounded, auditable failure. Retrying the same
                # request would only delay the rest of the queue.
                last_error = str(exc)
                break
            except Exception as exc:  # pragma: no cover - network-dependent
                last_error = str(exc)
                if attempt < retries:
                    time.sleep(3)
        if found is None:
            summary["failed"] += 1
            progress.setdefault("downloaded", {}).pop(identity, None)
            record_attempt(item, "failed", error=last_error or "search failed")
            progress["results"][identity] = {"status": "failed", "error": last_error, "at": now_iso()}
            atomic_write_json(progress_path, progress)
            atomic_write_json(inventory_path, inventory)
            continue
        if not found:
            summary["no_results"] += 1
            progress.setdefault("downloaded", {}).pop(identity, None)
            record_attempt(item, "no_results")
            progress["results"][identity] = {"status": "no_results", "at": now_iso()}
            atomic_write_json(progress_path, progress)
            atomic_write_json(inventory_path, inventory)
            continue

        best = choose_match(found, title, artist)
        if best is None:
            summary["no_results"] += 1
            progress.setdefault("downloaded", {}).pop(identity, None)
            audit = {
                "reason": "no_compatible_title_artist_match",
                "match_policy": MATCH_POLICY,
                "candidate_count": len(found),
                "rejected_candidates": rejected_candidate_metadata(found),
            }
            record_attempt(item, "no_results", **audit)
            progress["results"][identity] = {"status": "no_results", "at": now_iso(), **audit}
            atomic_write_json(progress_path, progress)
            inventory["generated_at"] = now_iso()
            inventory["counts"] = recompute_counts(inventory)
            atomic_write_json(inventory_path, inventory)
            continue

        matched = result_metadata(best)
        try:
            with item_timeout(item_timeout_seconds):
                downloaded = client.download([best])
            raw_path = None
            if downloaded:
                result = downloaded[0]
                raw_path = getattr(result, "save_path", None) or getattr(result, "saved_path", None)
            path = path_for_download(raw_path, audio_root) if raw_path else None
            if not path or not path.is_file():
                raise RuntimeError(f"musicdl 未返回已存在的文件路径: {raw_path!r}")
            item["download"] = {**file_record(path, audio_root), **matched}
            progress.setdefault("downloaded", {})[identity] = {
                "title": title,
                "artist": artist,
                "file": str(path),
                "ext": item["download"]["extension"],
                "size_bytes": item["download"]["size_bytes"],
                "time": item["download"]["recorded_at"],
                "play_link": candidate.get("play_link"),
                "platform": candidate.get("platform", "kugou"),
                "platform_track_key": candidate.get("platform_track_key"),
                "identity_key": candidate.get("identity_key"),
                **matched,
            }
            summary["downloaded"] += 1
            progress["results"][identity] = {
                "status": "downloaded",
                "path": item["download"]["path"],
                "at": now_iso(),
                **matched,
            }
            log(f"OK {path}")
        except Exception as exc:  # pragma: no cover - network/filesystem-dependent
            summary["failed"] += 1
            record_attempt(item, "failed", error=str(exc))
            progress["results"][identity] = {"status": "failed", "error": str(exc), "at": now_iso()}
            log(f"FAILED {exc}")
        atomic_write_json(progress_path, progress)
        inventory["generated_at"] = now_iso()
        inventory["counts"] = recompute_counts(inventory)
        atomic_write_json(inventory_path, inventory)
        if delay:
            time.sleep(delay)

    progress["finished_at"] = now_iso()
    progress["summary"] = summary
    atomic_write_json(progress_path, progress)
    inventory["generated_at"] = now_iso()
    inventory["counts"] = recompute_counts(inventory)
    atomic_write_json(inventory_path, inventory)
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def recompute_counts(inventory: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {"total": len(inventory.get("songs", []))}
    for song in inventory.get("songs", []):
        status = str(song.get("download", {}).get("status", "not_attempted"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True, help="musicdl work_dir; audio files go under KugouMusicClient/")
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--search-size", type=int, default=3)
    parser.add_argument(
        "--item-timeout-seconds",
        type=float,
        default=60.0,
        help="Maximum seconds for one musicdl search or download operation",
    )
    args = parser.parse_args()
    run_download(
        args.queue.expanduser(),
        args.inventory.expanduser(),
        args.work_dir.expanduser(),
        args.progress.expanduser(),
        args.log.expanduser(),
        args.run_id,
        args.max_items,
        args.dry_run,
        args.delay,
        args.retries,
        args.search_size,
        args.item_timeout_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

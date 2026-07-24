#!/usr/bin/env python3
"""Download explicitly queued retryable Kugou records through alternate musicdl sources."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MAX_FALLBACK_ATTEMPTS = 2


class ItemTimeoutError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def split_artists(value: Any) -> set[str]:
    return {normalize(part) for part in re.split(r"[、&,/]", str(value or "")) if normalize(part)}


def artists_match(target: str, found: str, aliases: list[list[str]]) -> bool:
    left, right = split_artists(target), split_artists(found)
    if left == right:
        return True
    for group in aliases:
        values = {normalize(item) for item in group}
        canonical = min(values) if values else ""
        left = {canonical if item in values else item for item in left}
        right = {canonical if item in values else item for item in right}
    return left == right


@contextmanager
def item_timeout(seconds: float):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return
    previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(ItemTimeoutError(f"timed out after {seconds:g}s")))
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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


def verify_file(path: Path, minimum_size: int, minimum_duration: float) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"file missing: {path}")
    size = path.stat().st_size
    if size <= minimum_size:
        raise RuntimeError(f"file too small: {size} bytes")
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        raise RuntimeError("ffprobe could not read duration")
    duration = float(probe.stdout.strip())
    if duration < minimum_duration:
        raise RuntimeError(f"duration too short: {duration:.3f}s")
    return {"size_bytes": size, "duration_seconds": duration}


def queue_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def find_item(inventory: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    identity = candidate.get("identity_key")
    title_key = candidate.get("title_artist_key")
    for song in inventory.get("songs", []):
        if identity and song.get("identity_key") == identity:
            return song
        if title_key and song.get("title_artist_key") == title_key:
            return song
    song = dict(candidate)
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


def fallback_attempt_limit(profile: dict[str, Any]) -> int:
    value = profile.get("max_fallback_attempts", DEFAULT_MAX_FALLBACK_ATTEMPTS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("profile.max_fallback_attempts must be a positive integer")
    return value


def fallback_attempt_result(
    previous_download: dict[str, Any] | None,
    *,
    attempt_status: str,
    retry_from_status: object,
    max_attempts: int,
    error: str | None = None,
    source: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Record one fallback round and return its durable terminal state.

    The count belongs to the inventory row, rather than a single run receipt,
    so a new weekly invocation cannot restart an automatic retry loop.
    """

    if attempt_status not in {"downloaded", "failed", "no_results"}:
        raise ValueError(f"unsupported fallback attempt status: {attempt_status}")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    prior = previous_download if isinstance(previous_download, dict) else {}
    raw_history = prior.get("fallback_history")
    history = [dict(entry) for entry in raw_history if isinstance(entry, dict)] if isinstance(raw_history, list) else []
    raw_attempts = prior.get("fallback_attempts")
    previous_attempts = raw_attempts if isinstance(raw_attempts, int) and not isinstance(raw_attempts, bool) and raw_attempts >= 0 else 0
    attempt = max(previous_attempts, len(history)) + 1
    recorded_at = now_iso()
    retry_status = str(retry_from_status or "").strip()
    entry: dict[str, Any] = {"attempt": attempt, "status": attempt_status, "at": recorded_at}
    if retry_status:
        entry["retry_from_status"] = retry_status
    if source:
        entry["source"] = source
    if error:
        entry["error"] = error
    history.append(entry)

    final_status = attempt_status
    metadata: dict[str, Any] = {
        "fallback": True,
        "fallback_attempts": attempt,
        "fallback_attempt_limit": max_attempts,
        "fallback_history": history,
        "last_fallback_status": attempt_status,
    }
    if retry_status:
        metadata["retry_from_status"] = retry_status
    if final_status != "downloaded" and attempt >= max_attempts:
        final_status = "abandoned"
        metadata.update(
            {
                "terminal_reason": "fallback_retry_limit_exhausted",
                "abandoned_at": recorded_at,
            }
        )
    return final_status, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    max_fallback_attempts = fallback_attempt_limit(profile)
    queue = queue_rows(args.queue)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    summary = {
        "run_id": args.run_id,
        "queue": len(queue),
        "downloaded": 0,
        "skipped_existing": 0,
        "failed": 0,
        "no_results": 0,
        "abandoned": 0,
        "max_fallback_attempts": max_fallback_attempts,
        "dry_run": args.dry_run,
    }
    progress = {"schema_version": 1, "run_id": args.run_id, "started_at": now_iso(), "results": {}}
    if args.dry_run:
        summary["would_process"] = len(queue)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    try:
        from musicdl.musicdl import MusicClient
    except Exception as exc:
        raise SystemExit(f"无法导入 musicdl: {exc}") from exc
    sources = list(profile["sources"])
    for candidate in queue:
        identity = candidate.get("identity_key") or candidate.get("title_artist_key")
        item = find_item(inventory, candidate)
        if downloaded_present(item):
            summary["skipped_existing"] += 1
            progress["results"][identity] = {"status": "skipped_existing", "at": now_iso()}
            continue
        previous_download = item.get("download") if isinstance(item.get("download"), dict) else None
        aliases = candidate.get("artist_aliases") or profile.get("artist_aliases", {}).get(f"{candidate.get('title')}\u0000{candidate.get('artist')}", [])
        retry_from_status = candidate.get("retry_from_status")
        result: dict[str, Any] = {
            "status": "failed",
            "source": None,
            "error": None,
            "retry_from_status": retry_from_status,
            "at": now_iso(),
        }
        matched = None
        for source in sources:
            try:
                client = MusicClient(
                    music_sources=[source],
                    init_music_clients_cfg={source: {"work_dir": str(args.work_dir)}},
                )
                with item_timeout(float(profile["source_timeout_seconds"])):
                    found = client.search(f"{candidate['title']} {candidate['artist']}").get(source, [])
                for song in found:
                    if normalize(getattr(song, "song_name", "")) == normalize(candidate["title"]) and artists_match(candidate["artist"], getattr(song, "singers", ""), aliases):
                        matched = (source, song)
                        break
                if matched:
                    break
            except Exception as exc:
                result["error"] = f"{source}: {exc}"
        if matched is None:
            final_status, retry_metadata = fallback_attempt_result(
                previous_download,
                attempt_status="no_results",
                retry_from_status=retry_from_status,
                max_attempts=max_fallback_attempts,
                error=result.get("error"),
            )
            summary[final_status] += 1
            result.update(
                {
                    "status": final_status,
                    "attempt_status": "no_results",
                    "fallback_attempts": retry_metadata["fallback_attempts"],
                }
            )
            if retry_metadata.get("terminal_reason"):
                result["terminal_reason"] = retry_metadata["terminal_reason"]
            item["download"] = {
                "status": final_status,
                "retention": "retained",
                "path": None,
                "recorded_at": now_iso(),
                **retry_metadata,
            }
        else:
            source, song = matched
            try:
                with item_timeout(float(profile["source_timeout_seconds"])):
                    downloaded = client.download([song])
                raw_path = getattr(downloaded[0], "save_path", None) or getattr(downloaded[0], "saved_path", None) or getattr(downloaded[0], "_save_path", None)
                path = Path(raw_path).expanduser() if raw_path else None
                if path and not path.is_absolute():
                    path = args.work_dir / path
                if path is None:
                    raise RuntimeError("musicdl returned no file path")
                verified = verify_file(path, int(profile["minimum_size_bytes"]), float(profile["minimum_duration_seconds"]))
                final_status, retry_metadata = fallback_attempt_result(
                    previous_download,
                    attempt_status="downloaded",
                    retry_from_status=retry_from_status,
                    max_attempts=max_fallback_attempts,
                    source=source,
                )
                item["download"] = {
                    "status": final_status,
                    "retention": "retained",
                    "path": str(path.resolve()),
                    "file_present": True,
                    "exists": True,
                    "source": source,
                    "matched_title": getattr(song, "song_name", None),
                    "matched_artist": getattr(song, "singers", None),
                    "recorded_at": now_iso(),
                    **retry_metadata,
                    **verified,
                }
                summary[final_status] += 1
                result.update(
                    {
                        "status": final_status,
                        "attempt_status": "downloaded",
                        "source": source,
                        "path": str(path.resolve()),
                        "fallback_attempts": retry_metadata["fallback_attempts"],
                        **verified,
                    }
                )
            except Exception as exc:
                error = str(exc)
                final_status, retry_metadata = fallback_attempt_result(
                    previous_download,
                    attempt_status="failed",
                    retry_from_status=retry_from_status,
                    max_attempts=max_fallback_attempts,
                    error=error,
                    source=source,
                )
                summary[final_status] += 1
                result.update(
                    {
                        "status": final_status,
                        "attempt_status": "failed",
                        "error": error,
                        "fallback_attempts": retry_metadata["fallback_attempts"],
                    }
                )
                if retry_metadata.get("terminal_reason"):
                    result["terminal_reason"] = retry_metadata["terminal_reason"]
                item["download"] = {
                    "status": final_status,
                    "retention": "retained",
                    "path": None,
                    "recorded_at": now_iso(),
                    "error": error,
                    **retry_metadata,
                }
        progress["results"][identity] = result
        inventory["generated_at"] = now_iso()
        inventory["counts"] = {"total": len(inventory.get("songs", []))}
        for song in inventory.get("songs", []):
            status = song.get("download", {}).get("status", "not_attempted")
            inventory["counts"][status] = inventory["counts"].get(status, 0) + 1
        atomic_write_json(args.inventory, inventory)
        atomic_write_json(args.progress, {**progress, "summary": summary})
    progress["finished_at"] = now_iso()
    progress["summary"] = summary
    atomic_write_json(args.progress, progress)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

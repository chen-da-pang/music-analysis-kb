#!/usr/bin/env python3
"""Download explicitly queued retryable Kugou records through alternate musicdl sources."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue as queue_module
import re
import signal
import subprocess
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
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

DEFAULT_PARALLEL_SOURCES = ["NeteaseDirect", "MiguMusicClient", "QQMusicClient", "KuwoMusicClient"]
DEFAULT_OUTER_CONCURRENCY = 8
DEFAULT_PER_SONG_DEADLINE_SECONDS = 120.0
DEFAULT_PARALLEL_SOURCE_TIMEOUT_SECONDS = 100.0
LOSSLESS_EXTENSIONS = {".flac", ".wav", ".ape", ".ogg", ".m4a"}
LOSSLESS_TRANSCODE_THRESHOLD_BYTES = 20_000_000
_NETEASE_DIRECT_MODULE = None




def call_with_timeout(fn, seconds):
    if seconds <= 0:
        return fn()
    box = {}

    def runner():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise ItemTimeoutError(f"timed out after {seconds:g}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _positive_number(profile, key, default):
    value = profile.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"profile.{key} must be a positive number")
    return float(value)


def _bounded_int(profile, key, default, maximum=32):
    value = profile.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise ValueError(f"profile.{key} must be an integer between 1 and {maximum}")
    return value


def parallel_settings(profile):
    enabled = profile.get("source_parallel") is True
    source_timeout = _positive_number(profile, "source_timeout_seconds", DEFAULT_PARALLEL_SOURCE_TIMEOUT_SECONDS)
    settings = {
        "source_parallel": enabled,
        "sources": list(profile["sources"]),
        "parallel_sources": list(profile.get("parallel_sources") or DEFAULT_PARALLEL_SOURCES),
        "per_song_deadline_seconds": _positive_number(profile, "per_song_deadline_seconds", DEFAULT_PER_SONG_DEADLINE_SECONDS),
        "outer_concurrency": _bounded_int(profile, "outer_concurrency", DEFAULT_OUTER_CONCURRENCY),
        "search_timeout_seconds": _positive_number(profile, "search_timeout_seconds", source_timeout),
        "download_timeout_seconds": _positive_number(profile, "download_timeout_seconds", source_timeout),
        "lossless_transcode_128k": profile.get("lossless_transcode_128k") is True,
    }
    if enabled:
        unsupported = [source for source in settings["parallel_sources"] if source not in DEFAULT_PARALLEL_SOURCES]
        if unsupported:
            raise ValueError(f"profile.parallel_sources contains unsupported sources: {unsupported}")
    return settings


def load_netease_direct():
    global _NETEASE_DIRECT_MODULE
    if _NETEASE_DIRECT_MODULE is None:
        module_path = Path(__file__).with_name("netease_direct.py")
        spec = importlib.util.spec_from_file_location("netease_direct", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _NETEASE_DIRECT_MODULE = module
    return _NETEASE_DIRECT_MODULE


def transcode_lossless_to_128k(path):
    source_size = path.stat().st_size
    source_ext = path.suffix.lower()
    target = path.with_name(path.stem + "-128k.mp3")
    temporary = path.with_name(path.stem + ".tmp128.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", "44100", "-b:a", "128k", str(temporary)],
        check=True,
        timeout=300,
    )
    temporary.replace(target)
    path.unlink(missing_ok=True)
    return target, {"transcoded_from_lossless": True, "source_size_bytes": source_size, "source_ext": source_ext}


def finalize_download(path, profile, settings):
    verified = verify_file(path, int(profile["minimum_size_bytes"]), float(profile["minimum_duration_seconds"]))
    metadata = {}
    if (
        settings["lossless_transcode_128k"]
        and path.suffix.lower() in LOSSLESS_EXTENSIONS
        and verified["size_bytes"] > LOSSLESS_TRANSCODE_THRESHOLD_BYTES
    ):
        path, metadata = transcode_lossless_to_128k(path)
        verified = {**verified, "size_bytes": path.stat().st_size}
    return path, verified, metadata


def attempt_musicdl_source(source, candidate, aliases, work_dir, profile, settings, music_client_cls):
    started = time.monotonic()
    outcome = {"attempt_status": "failed", "source": source, "error": None}
    try:
        client = music_client_cls(
            music_sources=[source],
            init_music_clients_cfg={source: {"work_dir": str(work_dir)}},
        )
        found = call_with_timeout(
            lambda: client.search(f"{candidate['title']} {candidate['artist']}").get(source, []),
            settings["search_timeout_seconds"],
        )
        matched = None
        for song in found or []:
            if normalize(getattr(song, "song_name", "")) == normalize(candidate["title"]) and artists_match(
                candidate["artist"], getattr(song, "singers", ""), aliases
            ):
                matched = song
                break
        if matched is None:
            outcome.update(attempt_status="no_results", error="no matching search result")
            return outcome
        downloaded = call_with_timeout(lambda: client.download([matched]), settings["download_timeout_seconds"])
        raw_path = (
            getattr(downloaded[0], "save_path", None)
            or getattr(downloaded[0], "saved_path", None)
            or getattr(downloaded[0], "_save_path", None)
        )
        download_path = Path(raw_path).expanduser() if raw_path else None
        if download_path and not download_path.is_absolute():
            download_path = work_dir / download_path
        if download_path is None:
            raise RuntimeError("musicdl returned no file path")
        download_path, verified, metadata = finalize_download(download_path, profile, settings)
        outcome.update(
            attempt_status="downloaded",
            path=str(download_path.resolve()),
            verified=verified,
            matched_title=getattr(matched, "song_name", None),
            matched_artist=getattr(matched, "singers", None),
            **metadata,
        )
    except ItemTimeoutError as exc:
        outcome["error"] = f"timeout: {exc}"
    except Exception as exc:  # noqa: BLE001
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        outcome["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return outcome


def attempt_netease_direct(candidate, aliases, work_dir, profile, settings):
    started = time.monotonic()
    outcome = {"attempt_status": "failed", "source": "NeteaseDirect", "error": None}
    try:
        netease_direct = load_netease_direct()
        netease_session = netease_direct.session()
        found = call_with_timeout(
            lambda: netease_direct.search(f"{candidate['title']} {candidate['artist']}", sess=netease_session),
            settings["search_timeout_seconds"],
        )
        matched = None
        for song in found or []:
            artists = "、".join(
                str(artist.get("name")) for artist in (song.get("ar") or []) if artist.get("name")
            )
            if normalize(song.get("name")) == normalize(candidate["title"]) and artists_match(
                candidate["artist"], artists, aliases
            ):
                matched = song
                break
        if matched is None:
            outcome.update(attempt_status="no_results", error="no matching netease result")
            return outcome
        info = call_with_timeout(
            lambda: netease_direct.resolve(matched["id"], "standard", sess=netease_session),
            settings["download_timeout_seconds"],
        )
        if not info.get("url"):
            outcome["error"] = "netease: no free download url (VIP or unavailable)"
            return outcome
        if info.get("freeTrialInfo"):
            outcome["error"] = "netease: free trial clip only, rejected"
            return outcome
        destination = work_dir / f"netease-{matched['id']}.mp3"
        call_with_timeout(
            lambda: netease_direct.download(info["url"], destination, sess=netease_session),
            settings["download_timeout_seconds"],
        )
        download_path, verified, metadata = finalize_download(destination, profile, settings)
        outcome.update(
            attempt_status="downloaded",
            path=str(download_path.resolve()),
            verified=verified,
            matched_title=matched.get("name"),
            matched_artist=artists,
            **metadata,
        )
    except ItemTimeoutError as exc:
        outcome["error"] = f"timeout: {exc}"
    except Exception as exc:  # noqa: BLE001
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        outcome["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return outcome


def download_from_source(source, candidate, aliases, work_dir, profile, settings, music_client_cls):
    if source == "NeteaseDirect":
        return attempt_netease_direct(candidate, aliases, work_dir, profile, settings)
    return attempt_musicdl_source(source, candidate, aliases, work_dir, profile, settings, music_client_cls)


def run_parallel_attempts(sources, attempt, deadline_seconds):
    results = queue_module.Queue()
    attempts = []

    def runner(source):
        try:
            results.put(attempt(source))
        except BaseException as exc:  # noqa: BLE001
            results.put({"attempt_status": "failed", "source": source, "error": f"{type(exc).__name__}: {exc}"})

    for source in sources:
        threading.Thread(target=runner, args=(source,), daemon=True).start()
    started = time.monotonic()
    deadline_hit = False
    while len(attempts) < len(sources):
        remaining = deadline_seconds - (time.monotonic() - started)
        if remaining <= 0:
            deadline_hit = True
            break
        try:
            outcome = results.get(timeout=remaining)
        except queue_module.Empty:
            deadline_hit = True
            break
        attempts.append(outcome)
        if outcome.get("attempt_status") == "downloaded":
            return outcome, attempts, False
    return None, attempts, deadline_hit


def combined_outcome(attempts, deadline_hit):
    errors = [f"{attempt.get('source')}: {attempt.get('error')}" for attempt in attempts if attempt.get("error")]
    if deadline_hit:
        return {
            "attempt_status": "failed",
            "source": None,
            "error": "per-song deadline exceeded; " + ("; ".join(errors) if errors else "no source finished"),
        }
    if any(attempt.get("attempt_status") == "failed" for attempt in attempts):
        return {"attempt_status": "failed", "source": None, "error": "; ".join(errors) or "all sources failed"}
    return {"attempt_status": "no_results", "source": None, "error": "; ".join(errors) or None}


def record_attempt_outcome(item, outcome, previous_download, retry_from_status, max_attempts):
    if outcome.get("attempt_status") == "downloaded":
        final_status, retry_metadata = fallback_attempt_result(
            previous_download,
            attempt_status="downloaded",
            retry_from_status=retry_from_status,
            max_attempts=max_attempts,
            source=outcome.get("source"),
        )
        download = {
            "status": final_status,
            "retention": "retained",
            "path": outcome.get("path"),
            "file_present": True,
            "exists": True,
            "source": outcome.get("source"),
            "matched_title": outcome.get("matched_title"),
            "matched_artist": outcome.get("matched_artist"),
            "recorded_at": now_iso(),
            **retry_metadata,
            **outcome.get("verified", {}),
        }
        for key in ("transcoded_from_lossless", "source_size_bytes", "source_ext"):
            if key in outcome:
                download[key] = outcome[key]
        item["download"] = download
        result = {
            "status": final_status,
            "attempt_status": "downloaded",
            "source": outcome.get("source"),
            "path": outcome.get("path"),
            "fallback_attempts": retry_metadata["fallback_attempts"],
            **outcome.get("verified", {}),
        }
        return final_status, result
    attempt_status = outcome.get("attempt_status")
    if attempt_status not in {"failed", "no_results"}:
        attempt_status = "failed"
    final_status, retry_metadata = fallback_attempt_result(
        previous_download,
        attempt_status=attempt_status,
        retry_from_status=retry_from_status,
        max_attempts=max_attempts,
        error=outcome.get("error"),
        source=outcome.get("source"),
    )
    item["download"] = {
        "status": final_status,
        "retention": "retained",
        "path": None,
        "recorded_at": now_iso(),
        "error": outcome.get("error"),
        **retry_metadata,
    }
    result = {
        "status": final_status,
        "attempt_status": attempt_status,
        "error": outcome.get("error"),
        "fallback_attempts": retry_metadata["fallback_attempts"],
    }
    if retry_metadata.get("terminal_reason"):
        result["terminal_reason"] = retry_metadata["terminal_reason"]
    return final_status, result


def compact_attempts(attempts):
    return [
        {
            "source": attempt.get("source"),
            "status": attempt.get("attempt_status"),
            "elapsed_ms": attempt.get("elapsed_ms"),
            "error": attempt.get("error"),
        }
        for attempt in attempts
    ]


def process_candidate(candidate, item, profile, settings, work_dir, max_attempts, music_client_cls):
    identity = candidate.get("identity_key") or candidate.get("title_artist_key")
    aliases = candidate.get("artist_aliases") or profile.get("artist_aliases", {}).get(
        f"{candidate.get('title')}{chr(0)}{candidate.get('artist')}", []
    )
    retry_from_status = candidate.get("retry_from_status")
    previous_download = item.get("download") if isinstance(item.get("download"), dict) else None
    started = time.monotonic()

    def attempt(source):
        return download_from_source(source, candidate, aliases, work_dir, profile, settings, music_client_cls)

    if settings["source_parallel"]:
        outcome, attempts, deadline_hit = run_parallel_attempts(
            settings["parallel_sources"], attempt, settings["per_song_deadline_seconds"]
        )
        if outcome is None:
            outcome = combined_outcome(attempts, deadline_hit)
        parallel_attempts = compact_attempts(attempts)
    else:
        attempts = []
        outcome = None
        for source in settings["sources"]:
            outcome = attempt(source)
            attempts.append(outcome)
            if outcome.get("attempt_status") == "downloaded":
                break
        if outcome is None or outcome.get("attempt_status") != "downloaded":
            errors = [f"{entry.get('source')}: {entry.get('error')}" for entry in attempts if entry.get("error")]
            outcome = {"attempt_status": "no_results", "source": None, "error": "; ".join(errors) or None}
        parallel_attempts = compact_attempts(attempts)
    final_status, result = record_attempt_outcome(item, outcome, previous_download, retry_from_status, max_attempts)
    result.update(
        {
            "retry_from_status": retry_from_status,
            "at": now_iso(),
            "parallel_attempts": parallel_attempts,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    )
    return identity, final_status, result


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
    settings = parallel_settings(profile)
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
        "source_parallel": settings["source_parallel"],
        "parallel_sources": settings["parallel_sources"] if settings["source_parallel"] else list(settings["sources"]),
        "outer_concurrency": settings["outer_concurrency"] if settings["source_parallel"] else 1,
        "per_song_deadline_seconds": settings["per_song_deadline_seconds"] if settings["source_parallel"] else None,
        "search_timeout_seconds": settings["search_timeout_seconds"],
        "download_timeout_seconds": settings["download_timeout_seconds"],
        "lossless_transcode_128k": settings["lossless_transcode_128k"],
        "dry_run": args.dry_run,
    }
    progress = {"schema_version": 2, "run_id": args.run_id, "started_at": now_iso(), "results": {}}
    if args.dry_run:
        summary["would_process"] = len(queue)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    try:
        from musicdl.musicdl import MusicClient
    except Exception as exc:
        raise SystemExit(f"无法导入 musicdl: {exc}") from exc
    items = []
    for candidate in queue:
        item = find_item(inventory, candidate)
        identity = candidate.get("identity_key") or candidate.get("title_artist_key")
        items.append((candidate, item, identity))
    write_lock = threading.Lock()

    def persist() -> None:
        inventory["generated_at"] = now_iso()
        counts = {"total": len(inventory.get("songs", []))}
        for song in inventory.get("songs", []):
            status = song.get("download", {}).get("status", "not_attempted")
            counts[status] = counts.get(status, 0) + 1
        inventory["counts"] = counts
        atomic_write_json(args.inventory, inventory)
        atomic_write_json(args.progress, {**progress, "summary": summary})

    def handle(candidate, item, identity) -> None:
        if downloaded_present(item):
            with write_lock:
                summary["skipped_existing"] += 1
                progress["results"][identity] = {"status": "skipped_existing", "at": now_iso()}
                persist()
            return
        identity, final_status, result = process_candidate(
            candidate, item, profile, settings, args.work_dir, max_fallback_attempts, MusicClient
        )
        with write_lock:
            progress["results"][identity] = result
            summary[final_status] += 1
            persist()

    try:
        if settings["source_parallel"]:
            with ThreadPoolExecutor(max_workers=settings["outer_concurrency"]) as pool:
                futures = [pool.submit(handle, candidate, item, identity) for candidate, item, identity in items]
                for future in futures:
                    future.result()
        else:
            for candidate, item, identity in items:
                handle(candidate, item, identity)
    finally:
        progress["finished_at"] = now_iso()
        progress["summary"] = summary
        atomic_write_json(args.progress, progress)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

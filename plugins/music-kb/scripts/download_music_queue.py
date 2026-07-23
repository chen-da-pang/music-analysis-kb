#!/usr/bin/env python3
"""Download only the songs in a prepared queue with musicdl.

This script is intentionally deterministic and is normally *run by Claude
Code*, not called directly by the publisher skill.  It updates the inventory
after every attempt so an interrupted run can be resumed without downloading
an already-present song again.
"""

from __future__ import annotations

import argparse
import hashlib
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
from typing import Any, Mapping


MATCH_POLICY = "exact_kugou_mix_song_id_title_compatible_artist_v2"
LYRIC_RECEIPT_SCHEMA_VERSION = 1
LYRIC_QUERY_METHOD = "musicdl_kugou_exact_mix_song_id_v1"
LYRIC_NORMALIZER_VERSION = "lrc-v1"

# These are exact platform-facing placeholder responses, not broad keyword
# guesses. An empty SongInfo.lyric is never enough to publish an exception.
INSTRUMENTAL_MARKERS = frozenset({"纯音乐，请欣赏", "纯音乐请欣赏", "此歌曲为纯音乐，请您欣赏"})
PLATFORM_UNAVAILABLE_MARKERS = frozenset({"暂无歌词", "该歌曲暂无歌词", "暂无歌词，敬请期待"})
NON_LYRIC_PAYLOAD_MARKERS = ("<script", "<html", "<!doctype", "获取失败")
_TIMESTAMP_PREFIX = re.compile(r"^\s*(?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+\s*")
_METADATA_LINE = re.compile(
    r"^\s*\[(?:ar|al|ti|by|offset|re|ve|tool|length|au):[^\]]*\]\s*$",
    re.IGNORECASE,
)


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


def _search_payload(item: Any) -> Mapping[str, Any]:
    raw_data = getattr(item, "raw_data", None)
    if not isinstance(raw_data, Mapping):
        return {}
    search = raw_data.get("search")
    return search if isinstance(search, Mapping) else {}


def result_kugou_track_keys(item: Any) -> set[str]:
    """Return the exact mix-song identifiers carried by musicdl's raw result."""

    search = _search_payload(item)
    return {
        str(search[key]).strip()
        for key in ("MixSongID", "mixsongid", "EMixSongID", "emixsongid", "ID", "id")
        if search.get(key) is not None and str(search[key]).strip()
    }


def expected_kugou_track_key(candidate: Mapping[str, Any]) -> str | None:
    """Resolve a queue row's exact Kugou mix-song key without title fallback."""

    direct = str(candidate.get("platform_track_key") or "").strip()
    if direct:
        return direct
    identity = str(candidate.get("identity_key") or "").strip()
    if identity.startswith("kugou:") and identity.split(":", 1)[1].strip():
        return identity.split(":", 1)[1].strip()
    source_track_id = str(candidate.get("source_track_id") or "").strip()
    if source_track_id.startswith("kugou-") and source_track_id.removeprefix("kugou-").strip():
        return source_track_id.removeprefix("kugou-").strip()
    return None


def source_track_identity(candidate: Mapping[str, Any], expected_track_key: str | None) -> str | None:
    explicit = str(candidate.get("source_track_id") or "").strip()
    if explicit:
        return explicit
    return f"kugou-{expected_track_key}" if expected_track_key else None


def source_name(candidate: Mapping[str, Any]) -> str:
    return str(candidate.get("source_name") or candidate.get("platform") or "kugou").strip().lower()


def choose_match(
    results: list[Any],
    title: str,
    artist: str,
    *,
    platform_track_key: str | None = None,
) -> Any | None:
    """Return a title/artist-compatible result with an exact platform identity.

    Title and artist remain useful for candidate discovery, but they cannot
    prove a lyric or downloaded audio belongs to a canonical source track.
    Passing ``platform_track_key`` therefore requires a matching Kugou
    ``MixSongID``/``ID`` in the raw musicdl response.
    """

    wanted_title = normalize_title(title)
    for item in results:
        item_title = normalize_title(getattr(item, "song_name", ""))
        if item_title != wanted_title or not artists_match(artist, getattr(item, "singers", "")):
            continue
        if platform_track_key is not None and platform_track_key not in result_kugou_track_keys(item):
            continue
        return item
    return None


def result_metadata(item: Any) -> dict[str, Any]:
    return {
        "source": str(getattr(item, "source", None) or "KugouMusicClient"),
        "matched_title": str(getattr(item, "song_name", "") or ""),
        "matched_artist": str(getattr(item, "singers", "") or ""),
        "matched_kugou_track_keys": sorted(result_kugou_track_keys(item)),
        "matched_file_hash": str(getattr(item, "identifier", "") or ""),
        "match_policy": MATCH_POLICY,
    }


def rejected_candidate_metadata(results: list[Any], limit: int = 10) -> list[dict[str, str]]:
    rejected: list[dict[str, str]] = []
    for item in results[:limit]:
        entry = {
            "title": str(getattr(item, "song_name", "") or ""),
            "artist": str(getattr(item, "singers", "") or ""),
        }
        track_keys = sorted(result_kugou_track_keys(item))
        if track_keys:
            entry["kugou_track_keys"] = ",".join(track_keys)
        rejected.append(entry)
    return rejected


def normalize_lyric_text(value: Any) -> str:
    """Keep lyric text readable while removing only LRC transport markers."""

    if not isinstance(value, str):
        return ""
    lines: list[str] = []
    for raw_line in value.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _METADATA_LINE.fullmatch(raw_line):
            continue
        line = _TIMESTAMP_PREFIX.sub("", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def lyric_text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = repr(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _lyric_response(item: Any) -> Any:
    raw_data = getattr(item, "raw_data", None)
    return raw_data.get("lyric") if isinstance(raw_data, Mapping) else None


def _marker(value: str) -> str:
    return re.sub(r"[\s，,。.!！]", "", value).strip()


def _explicitly_no_platform_lyric(response: Any) -> bool:
    return isinstance(response, Mapping) and isinstance(response.get("candidates"), list) and not response["candidates"]


def _is_non_lyric_payload(value: str) -> bool:
    folded = value.casefold()
    return any(marker.casefold() in folded for marker in NON_LYRIC_PAYLOAD_MARKERS)


def lyric_receipt_for_match(
    candidate: Mapping[str, Any],
    item: Any | None,
    *,
    run_id: str,
    reason: str | None = None,
    response_kind: str | None = None,
) -> dict[str, Any]:
    """Create an auditable receipt for one exact-result lyric attempt.

    Missing lyrics remain ``pending`` unless the Kugou response explicitly
    reports zero lyric candidates or returns an exact, known exception marker.
    Network, parsing, and identity errors therefore cannot masquerade as
    platform-unavailable content.
    """

    expected_track_key = expected_kugou_track_key(candidate)
    receipt_source_name = source_name(candidate)
    receipt_source_track_id = source_track_identity(candidate, expected_track_key)
    if not receipt_source_track_id:
        receipt_source_track_id = str(candidate.get("identity_key") or "").strip()
    result_keys = sorted(result_kugou_track_keys(item)) if item is not None else []
    response = _lyric_response(item) if item is not None else None
    raw_lyric = getattr(item, "lyric", None) if item is not None else None
    readable = normalize_lyric_text(raw_lyric)
    status = "pending"
    response_kind = response_kind or ("query_error" if reason else "missing_lyric")
    final_reason = reason or "exact platform result did not return a publishable lyric response"
    lyric_text: str | None = None

    if item is not None and expected_track_key and expected_track_key not in result_keys:
        response_kind = "identity_mismatch"
        final_reason = "musicdl result identity does not match the requested Kugou mix-song ID"
    elif item is not None and readable:
        marker = _marker(readable)
        if marker in {_marker(value) for value in INSTRUMENTAL_MARKERS}:
            status = "instrumental"
            response_kind = "platform_instrumental_marker"
            final_reason = "exact Kugou lyric response explicitly marks this recording as instrumental"
        elif marker in {_marker(value) for value in PLATFORM_UNAVAILABLE_MARKERS}:
            status = "platform_unavailable"
            response_kind = "platform_unavailable_marker"
            final_reason = "exact Kugou lyric response explicitly reports lyrics unavailable"
        elif _is_non_lyric_payload(readable):
            response_kind = "invalid_lyric_payload"
            final_reason = "exact Kugou lyric response was an HTML or failure placeholder, not lyrics"
        else:
            status = "available"
            response_kind = "lyrics_available"
            final_reason = "exact Kugou source identity returned lyric text"
            lyric_text = readable
    elif item is not None and _explicitly_no_platform_lyric(response):
        status = "platform_unavailable"
        response_kind = "platform_zero_lyric_candidates"
        final_reason = "exact Kugou lyric endpoint explicitly returned zero lyric candidates"
    elif item is not None and raw_lyric is not None:
        response_kind = "empty_after_normalization"
        final_reason = "exact Kugou result returned lyric data that was empty after LRC normalization"

    evidence = {
        "source_name": receipt_source_name,
        "source_track_id": receipt_source_track_id,
        "reason": final_reason,
        "query_method": LYRIC_QUERY_METHOD,
        "response_kind": response_kind,
        "run_id": run_id,
        "expected_kugou_mix_song_id": expected_track_key,
        "returned_kugou_mix_song_ids": result_keys,
        "returned_file_hash": str(getattr(item, "identifier", "") or "") if item is not None else None,
        "raw_response_sha256": _json_sha256(response),
    }
    receipt: dict[str, Any] = {
        "schema_version": LYRIC_RECEIPT_SCHEMA_VERSION,
        "run_id": run_id,
        "source_name": receipt_source_name,
        "source_track_id": receipt_source_track_id,
        "status": status,
        "normalizer_version": LYRIC_NORMALIZER_VERSION,
        "evidence": evidence,
    }
    for optional_key in ("recording_id", "source_track_row_id", "identity_key", "platform_track_key"):
        if candidate.get(optional_key) is not None:
            receipt[optional_key] = candidate[optional_key]
    if lyric_text is not None:
        receipt["lyric_text"] = lyric_text
        receipt["text_sha256"] = lyric_text_sha256(lyric_text)
    return receipt


def append_lyric_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    """Append one durable worker receipt without depending on an LRC file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(receipt), handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def lyric_progress_metadata(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": str(receipt.get("status") or "pending"),
        "source_name": receipt.get("source_name"),
        "source_track_id": receipt.get("source_track_id"),
        "text_sha256": receipt.get("text_sha256"),
        "response_kind": (
            receipt.get("evidence", {}).get("response_kind")
            if isinstance(receipt.get("evidence"), Mapping)
            else None
        ),
        "at": now_iso(),
    }


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
    lyrics_receipt_path: Path | None = None,
) -> dict[str, Any]:
    if item_timeout_seconds <= 0:
        raise ValueError("item-timeout-seconds must be positive")
    queue = load_queue(queue_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    lyrics_receipt_path = (
        lyrics_receipt_path.expanduser()
        if lyrics_receipt_path is not None
        else progress_path.parent / "lyrics-receipts.jsonl"
    )
    audio_root = work_dir / "KugouMusicClient"
    audio_root.mkdir(parents=True, exist_ok=True)
    progress = {
        "schema_version": 1,
        "run_id": run_id,
        "queue": str(queue_path.resolve()),
        "started_at": now_iso(),
        "downloaded": {},
        "results": {},
        "lyrics": {},
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
    progress.setdefault("lyrics", {})

    summary = {
        "run_id": run_id,
        "queue": len(queue),
        "downloaded": 0,
        "skipped_existing": 0,
        "failed": 0,
        "no_results": 0,
        "lyrics_available": 0,
        "lyrics_instrumental": 0,
        "lyrics_platform_unavailable": 0,
        "lyrics_pending": 0,
        "lyrics_receipt": str(lyrics_receipt_path.resolve()),
        "dry_run": dry_run,
    }
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
        expected_track_key = expected_kugou_track_key(candidate)
        if not expected_track_key:
            summary["no_results"] += 1
            progress.setdefault("downloaded", {}).pop(identity, None)
            audit = {
                "reason": "missing_kugou_platform_track_identity",
                "match_policy": MATCH_POLICY,
            }
            record_attempt(item, "no_results", **audit)
            progress["results"][identity] = {"status": "no_results", "at": now_iso(), **audit}
            atomic_write_json(progress_path, progress)
            inventory["generated_at"] = now_iso()
            inventory["counts"] = recompute_counts(inventory)
            atomic_write_json(inventory_path, inventory)
            continue
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

        best = choose_match(found, title, artist, platform_track_key=expected_track_key)
        if best is None:
            summary["no_results"] += 1
            progress.setdefault("downloaded", {}).pop(identity, None)
            audit = {
                "reason": "no_exact_platform_identity_title_artist_match",
                "match_policy": MATCH_POLICY,
                "expected_kugou_mix_song_id": expected_track_key,
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
            lyric_source = result if downloaded and getattr(result, "raw_data", None) is not None else best
            receipt = lyric_receipt_for_match(candidate, lyric_source, run_id=run_id)
            try:
                append_lyric_receipt(lyrics_receipt_path, receipt)
                progress["lyrics"][identity] = lyric_progress_metadata(receipt)
                summary[f"lyrics_{receipt['status']}"] += 1
            except OSError as lyric_error:
                progress["lyrics"][identity] = {
                    "status": "pending",
                    "error": f"could not persist lyric receipt: {lyric_error}",
                    "at": now_iso(),
                }
                summary["lyrics_pending"] += 1
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


def _queue_identity(candidate: Mapping[str, Any]) -> str:
    for key in ("source_track_id", "identity_key", "title_artist_key"):
        value = str(candidate.get(key) or "").strip()
        if value:
            return value
    raise ValueError("queue row has no source_track_id, identity_key, or title_artist_key")


def run_lyrics_only(
    queue_path: Path,
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
    lyrics_receipt_path: Path,
) -> dict[str, Any]:
    """Query lyrics through the same Kugou worker without touching audio/inventory."""

    if item_timeout_seconds <= 0:
        raise ValueError("item-timeout-seconds must be positive")
    queue = load_queue(queue_path)
    lyrics_receipt_path = lyrics_receipt_path.expanduser()
    progress = {
        "schema_version": 1,
        "mode": "lyrics_only",
        "run_id": run_id,
        "queue": str(queue_path.resolve()),
        "started_at": now_iso(),
        "results": {},
    }
    if progress_path.exists():
        try:
            old = json.loads(progress_path.read_text(encoding="utf-8"))
            if old.get("run_id") == run_id and old.get("mode") == "lyrics_only":
                progress = old
        except (ValueError, OSError):
            pass
    progress.pop("finished_at", None)
    progress.pop("summary", None)
    progress.setdefault("results", {})

    summary = {
        "run_id": run_id,
        "mode": "lyrics_only",
        "queue": len(queue),
        "attempted": 0,
        "skipped_terminal": 0,
        "available": 0,
        "instrumental": 0,
        "platform_unavailable": 0,
        "pending": 0,
        "lyrics_receipt": str(lyrics_receipt_path.resolve()),
        "dry_run": dry_run,
    }
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

    terminal_statuses = {"available", "instrumental", "platform_unavailable"}
    for index, candidate in enumerate(selected, start=1):
        identity = _queue_identity(candidate)
        existing = progress["results"].get(identity)
        if isinstance(existing, Mapping) and existing.get("lyric_status") in terminal_statuses:
            summary["skipped_terminal"] += 1
            continue
        title = str(candidate.get("title") or "").strip()
        artist = str(candidate.get("artist") or "").strip()
        expected_track_key = expected_kugou_track_key(candidate)
        summary["attempted"] += 1
        log(f"lyrics-only [{index}/{len(selected)}] {title} - {artist}")
        receipt: dict[str, Any]
        if not title or not artist:
            receipt = lyric_receipt_for_match(
                candidate,
                None,
                run_id=run_id,
                reason="queue row has no title or artist for candidate discovery",
                response_kind="invalid_queue_row",
            )
        elif not expected_track_key:
            receipt = lyric_receipt_for_match(
                candidate,
                None,
                run_id=run_id,
                reason="source_track_id cannot be converted to an exact Kugou mix-song ID",
                response_kind="unsupported_source_identity",
            )
        else:
            found: list[Any] | None = None
            last_error: str | None = None
            for attempt in range(retries + 1):
                try:
                    with item_timeout(item_timeout_seconds):
                        found = client.search(f"{title} {artist}").get("KugouMusicClient", [])
                    break
                except ItemTimeoutError as exc:
                    last_error = str(exc)
                    break
                except Exception as exc:  # pragma: no cover - network-dependent
                    last_error = str(exc)
                    if attempt < retries:
                        time.sleep(3)
            if found is None:
                receipt = lyric_receipt_for_match(
                    candidate,
                    None,
                    run_id=run_id,
                    reason=last_error or "musicdl search failed",
                    response_kind="network_or_parse_error",
                )
            else:
                best = choose_match(
                    found,
                    title,
                    artist,
                    platform_track_key=expected_track_key,
                )
                if best is None:
                    receipt = lyric_receipt_for_match(
                        candidate,
                        None,
                        run_id=run_id,
                        reason="no title/artist-compatible musicdl result carried the requested Kugou mix-song ID",
                        response_kind="identity_mismatch",
                    )
                else:
                    receipt = lyric_receipt_for_match(candidate, best, run_id=run_id)
        append_lyric_receipt(lyrics_receipt_path, receipt)
        progress["results"][identity] = {
            "query_status": "completed",
            "lyric_status": receipt["status"],
            "source_track_id": receipt["source_track_id"],
            "text_sha256": receipt.get("text_sha256"),
            "response_kind": receipt["evidence"]["response_kind"],
            "at": now_iso(),
        }
        summary[str(receipt["status"])] += 1
        atomic_write_json(progress_path, progress)
        if delay:
            time.sleep(delay)

    progress["finished_at"] = now_iso()
    progress["summary"] = summary
    atomic_write_json(progress_path, progress)
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
    parser.add_argument("--inventory", type=Path, help="Required for audio-download mode; unused by --lyrics-only")
    parser.add_argument("--work-dir", type=Path, required=True, help="musicdl work_dir; audio files go under KugouMusicClient/")
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--lyrics-only",
        action="store_true",
        help="Fetch exact-identity lyrics without downloading audio or modifying inventory",
    )
    parser.add_argument(
        "--lyrics-receipt",
        type=Path,
        help="Append-only JSONL receipt destination (defaults beside --progress)",
    )
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
    queue = args.queue.expanduser()
    work_dir = args.work_dir.expanduser()
    progress = args.progress.expanduser()
    log = args.log.expanduser()
    lyrics_receipt = args.lyrics_receipt.expanduser() if args.lyrics_receipt else progress.parent / "lyrics-receipts.jsonl"
    if args.lyrics_only:
        run_lyrics_only(
            queue,
            work_dir,
            progress,
            log,
            args.run_id,
            args.max_items,
            args.dry_run,
            args.delay,
            args.retries,
            args.search_size,
            args.item_timeout_seconds,
            lyrics_receipt,
        )
        return 0
    if args.inventory is None:
        parser.error("--inventory is required unless --lyrics-only is used")
    run_download(
        queue,
        args.inventory.expanduser(),
        work_dir,
        progress,
        log,
        args.run_id,
        args.max_items,
        args.dry_run,
        args.delay,
        args.retries,
        args.search_size,
        args.item_timeout_seconds,
        lyrics_receipt,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

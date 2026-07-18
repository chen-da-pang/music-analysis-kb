#!/usr/bin/env python3
"""Build the durable song/download inventory used by weekly updates.

The inventory is deliberately separate from the publisher SQLite database.  It
records the stable Kugou identity, chart appearances, and the local audio file
state so a future download run can skip songs that are already present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def title_artist_key(platform: str, title: Any, artist: Any) -> str:
    return f"{platform}:{normalize(title)}\x00{normalize(artist)}"


def identity_key(platform: str, track_key: Any) -> str:
    return f"{platform}:{str(track_key or '').strip()}"


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


def file_metadata(path: Path, hash_file: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exists": path.is_file(),
        "size_bytes": None,
        "mtime_ns": None,
        "sha256": None,
    }
    if not path.is_file():
        return result
    stat = path.stat()
    result["size_bytes"] = stat.st_size
    result["mtime_ns"] = stat.st_mtime_ns
    if hash_file:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        result["sha256"] = digest.hexdigest()
    return result


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else None


def progress_by_title(progress_path: Path) -> dict[str, dict[str, Any]]:
    """Read the historical Claude Code/musicdl progress format.

    The July 6 run keyed records by an MD5 of title+artist.  The inventory
    intentionally re-keys those records by a readable normalized title/artist
    key so future runs do not depend on that implementation detail.
    """

    data = load_json(progress_path) or {}
    result: dict[str, dict[str, Any]] = {}
    downloaded = data.get("downloaded", {})
    if not isinstance(downloaded, dict):
        return result
    for record in downloaded.values():
        if not isinstance(record, dict):
            continue
        title = record.get("title")
        artist = record.get("artist")
        if title and artist:
            result[title_artist_key("kugou", title, artist)] = record
    return result


def relative_audio_path(path: Path, audio_root: Path) -> str:
    try:
        return path.resolve().relative_to(audio_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def audio_retention_summary(previous: dict[str, Any], songs: list[dict[str, Any]]) -> dict[str, Any]:
    """Preserve auditable purge metadata while reflecting mixed new batches."""

    if not songs:
        return {}
    retention = [
        str(song.get("download", {}).get("retention", "retained"))
        for song in songs
    ]
    if all(value == "purged_after_analysis" for value in retention):
        deleted_at = previous.get("audio_files_deleted_at")
        if not deleted_at:
            purged_times = [
                song.get("download", {}).get("purged_at")
                for song in songs
                if song.get("download", {}).get("purged_at")
            ]
            deleted_at = max(purged_times) if purged_times else None
        result: dict[str, Any] = {"audio_retention": "purged_after_analysis"}
        if deleted_at:
            result["audio_files_deleted_at"] = deleted_at
        return result
    if any(value == "retained" for value in retention):
        return {"audio_retention": "retained", "audio_files_deleted_at": None}
    return {"audio_retention": "mixed", "audio_files_deleted_at": None}


def previous_download(record: dict[str, Any] | None, audio_root: Path, hash_file: bool) -> dict[str, Any]:
    if not record:
        return {"status": "not_attempted", "path": None, **file_metadata(Path("/does/not/exist"), False)}
    if record.get("retention") == "purged_after_analysis":
        preserved = dict(record)
        preserved["exists"] = False
        preserved["file_present"] = False
        return preserved
    raw_path = record.get("path") or record.get("file")
    path = Path(str(raw_path)).expanduser() if raw_path else Path("/does/not/exist")
    if not path.is_absolute():
        path = audio_root / path
    meta = file_metadata(path, hash_file)
    status = "downloaded" if meta["exists"] else "missing"
    return {
        "status": status,
        "path": relative_audio_path(path, audio_root) if meta["exists"] else str(raw_path) if raw_path else None,
        "extension": path.suffix.lower().lstrip(".") or record.get("ext"),
        "file_present": meta["exists"],
        "recorded_at": record.get("time") or record.get("downloaded_at"),
        **meta,
    }


def load_db_songs(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
              s.song_id,
              pt.platform,
              pt.platform_track_key,
              s.canonical_title AS title,
              s.canonical_artist AS artist,
              pt.play_link,
              ce.rank,
              c.chart_id,
              c.chart_name,
              cr.run_id,
              cr.generated_at,
              cr.source
            FROM songs AS s
            JOIN platform_tracks AS pt ON pt.song_id = s.song_id
            LEFT JOIN chart_entries AS ce
              ON ce.platform = pt.platform
             AND ce.platform_track_key = pt.platform_track_key
            LEFT JOIN charts AS c ON c.chart_id = ce.chart_id
            LEFT JOIN chart_runs AS cr ON cr.run_id = ce.run_id
            WHERE pt.platform = 'kugou'
            ORDER BY s.song_id, cr.generated_at DESC, ce.rank
            """
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        platform = str(row["platform"])
        track_key = str(row["platform_track_key"])
        key = identity_key(platform, track_key)
        if key not in grouped:
            grouped[key] = {
                "identity_key": key,
                "title_artist_key": title_artist_key(platform, row["title"], row["artist"]),
                "platform": platform,
                "platform_track_key": track_key,
                "song_id": row["song_id"],
                "title": row["title"],
                "artist": row["artist"],
                "play_link": row["play_link"],
                "source_runs": [],
                "chart_appearances": [],
            }
        item = grouped[key]
        if row["run_id"]:
            run = {
                "run_id": row["run_id"],
                "generated_at": row["generated_at"],
                "source": row["source"],
            }
            if run not in item["source_runs"]:
                item["source_runs"].append(run)
        if row["chart_id"]:
            item["chart_appearances"].append(
                {
                    "chart_id": row["chart_id"],
                    "chart_name": row["chart_name"],
                    "rank": row["rank"],
                    "run_id": row["run_id"],
                }
            )
    return list(grouped.values())


def build_inventory(
    db_path: Path,
    progress_path: Path,
    inventory_path: Path,
    audio_root: Path,
    hash_file: bool = False,
) -> dict[str, Any]:
    previous = load_json(inventory_path) or {}
    old_songs = {
        str(song.get("identity_key")): song
        for song in previous.get("songs", [])
        if isinstance(song, dict) and song.get("identity_key")
    }
    old_by_title = {
        str(song.get("title_artist_key")): song
        for song in old_songs.values()
        if song.get("title_artist_key")
    }
    legacy_progress = progress_by_title(progress_path)
    songs = load_db_songs(db_path)
    seen: set[str] = set()

    for song in songs:
        key = song["identity_key"]
        seen.add(key)
        old = old_songs.get(key) or old_by_title.get(song["title_artist_key"])
        download = None
        if old and isinstance(old.get("download"), dict):
            download = old["download"]
        if not download or download.get("status") != "downloaded":
            legacy = legacy_progress.get(song["title_artist_key"])
            if legacy:
                download = previous_download(legacy, audio_root, hash_file)
            elif old and isinstance(old.get("download"), dict):
                # Preserve failed/no-result audit state until a later queue
                # attempt replaces it with a fresh result.
                download = dict(old["download"])
            else:
                download = previous_download(None, audio_root, hash_file)
        elif download.get("path"):
            download = previous_download(download, audio_root, hash_file)
        song["download"] = download
        song["last_seen_at"] = now_iso()

    # Keep historical songs in the catalog even when they fall out of the
    # latest chart run. They remain dedupe candidates for future updates.
    for key, song in old_songs.items():
        if key not in seen:
            song = dict(song)
            if isinstance(song.get("download"), dict) and song["download"].get("path"):
                song["download"] = previous_download(song["download"], audio_root, hash_file)
            songs.append(song)

    songs.sort(key=lambda item: (str(item.get("title", "")).casefold(), str(item.get("artist", "")).casefold()))
    counts = defaultdict(int)
    for song in songs:
        counts[str(song.get("download", {}).get("status", "not_attempted"))] += 1
    result = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "source_db": str(db_path.resolve()),
        "legacy_progress": str(progress_path.resolve()),
        "audio_root": str(audio_root.resolve()),
        "counts": {"total": len(songs), **dict(sorted(counts.items()))},
        "songs": songs,
    }
    result.update(audio_retention_summary(previous, songs))
    atomic_write_json(inventory_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--hash", action="store_true", dest="hash_file", help="Hash every existing audio file")
    args = parser.parse_args()
    result = build_inventory(
        args.db.expanduser(),
        args.progress.expanduser(),
        args.inventory.expanduser(),
        args.audio_root.expanduser(),
        args.hash_file,
    )
    print(json.dumps({"inventory": str(args.inventory), "counts": result["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

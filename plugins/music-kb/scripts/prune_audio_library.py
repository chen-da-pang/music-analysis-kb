#!/usr/bin/env python3
"""Delete only locally present audio proven to be in the verified knowledge base.

The inventory remains the deduplication authority. Purged records keep their
identity and historical path but are marked ``purged_after_analysis``; future
queue preparation therefore skips them without requiring the audio file.
Downloaded tracks without a matching source-track entry remain untouched for
future Music Flamingo analysis.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def validate_release(knowledge_db: Path) -> tuple[dict[str, int], dict[str, set[str]]]:
    connection = sqlite3.connect(f"{knowledge_db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        counts = {
            "campaign_delivery_provenance": connection.execute(
                "SELECT COUNT(*) FROM campaign_delivery_provenance"
            ).fetchone()[0],
            "source_tracks": connection.execute("SELECT COUNT(*) FROM source_track").fetchone()[0],
            "source_links": connection.execute(
                "SELECT COUNT(*) FROM source_track WHERE source_url IS NOT NULL AND trim(source_url) <> ''"
            ).fetchone()[0],
        }
        lyric_counts = connection.execute(
            """
            SELECT COUNT(*) AS canonical_recordings,
                   COALESCE(SUM(CASE WHEN rl.status = 'available' THEN 1 ELSE 0 END), 0) AS lyrics_available,
                   COALESCE(SUM(CASE WHEN rl.status = 'instrumental' THEN 1 ELSE 0 END), 0) AS lyrics_instrumental,
                   COALESCE(SUM(CASE WHEN rl.status = 'platform_unavailable' THEN 1 ELSE 0 END), 0) AS lyrics_platform_unavailable,
                   COALESCE(SUM(CASE WHEN rl.status = 'pending' THEN 1 ELSE 0 END), 0) AS lyrics_pending,
                   COALESCE(SUM(CASE WHEN rl.recording_id IS NULL THEN 1 ELSE 0 END), 0) AS lyrics_missing
            FROM recording r
            LEFT JOIN recording_lyric rl ON rl.recording_id = r.id
            WHERE r.canonical_analysis_id IS NOT NULL
            """
        ).fetchone()
        lyric_count_keys = (
            "canonical_recordings",
            "lyrics_available",
            "lyrics_instrumental",
            "lyrics_platform_unavailable",
            "lyrics_pending",
            "lyrics_missing",
        )
        counts.update(
            {
                key: int(value or 0)
                for key, value in zip(lyric_count_keys, lyric_counts, strict=True)
            }
        )
        counts["lyrics_unresolved"] = (
            counts["canonical_recordings"]
            - counts["lyrics_available"]
            - counts["lyrics_instrumental"]
            - counts["lyrics_platform_unavailable"]
        )
        source_ids: dict[str, set[str]] = {}
        for source_name, source_track_id in connection.execute(
            "SELECT lower(source_name), source_track_id FROM source_track"
        ):
            source_ids.setdefault(str(source_name), set()).add(str(source_track_id))
    except sqlite3.Error as exc:
        raise RuntimeError(f"知识库缺少可验证的链接/来源表，不能删除音频: {exc}") from exc
    finally:
        connection.close()
    normalized = {key: int(value) for key, value in counts.items()}
    if normalized["campaign_delivery_provenance"] <= 0:
        raise RuntimeError("知识库没有 campaign delivery provenance，不能删除音频")
    if normalized["source_tracks"] <= 0 or normalized["source_links"] != normalized["source_tracks"]:
        raise RuntimeError(
            "知识库 source link 不完整，不能删除音频: "
            f"source_tracks={normalized['source_tracks']} source_links={normalized['source_links']}"
        )
    if normalized["lyrics_unresolved"]:
        raise RuntimeError(
            "知识库歌词覆盖不完整，不能删除音频: "
            f"canonical_recordings={normalized['canonical_recordings']} "
            f"lyrics_available={normalized['lyrics_available']} "
            f"lyrics_instrumental={normalized['lyrics_instrumental']} "
            f"lyrics_platform_unavailable={normalized['lyrics_platform_unavailable']} "
            f"lyrics_pending={normalized['lyrics_pending']} "
            f"lyrics_missing={normalized['lyrics_missing']} "
            f"lyrics_unresolved={normalized['lyrics_unresolved']}"
        )
    return normalized, source_ids


def source_track_is_analyzed(song: dict[str, Any], source_ids: dict[str, set[str]]) -> bool:
    platform = str(song.get("platform") or "kugou").strip().lower()
    raw_id = str(song.get("platform_track_key") or "").strip()
    identity_key = str(song.get("identity_key") or "").strip()
    if not raw_id and ":" in identity_key:
        raw_id = identity_key.split(":", 1)[1]
    candidates = {identity_key, raw_id}
    if raw_id:
        candidates.update({f"{platform}-{raw_id}", f"{platform}:{raw_id}"})
    return any(candidate in source_ids.get(platform, set()) for candidate in candidates if candidate)


def prune(
    inventory_path: Path,
    audio_root: Path,
    knowledge_db: Path,
    expected_count: int,
    confirm: bool,
) -> dict[str, Any]:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    songs = [song for song in inventory.get("songs", []) if isinstance(song, dict)]
    if len(songs) != expected_count:
        raise RuntimeError(f"inventory 歌曲数为 {len(songs)}，不是预期的 {expected_count}")
    release_counts, source_ids = validate_release(knowledge_db)
    root = audio_root.expanduser().resolve()
    present_paths: dict[int, Path] = {}
    directory_owners: dict[Path, set[int]] = {}
    for index, song in enumerate(songs):
        relative_path = str(song.get("download", {}).get("path") or "").strip()
        if not relative_path:
            continue
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"inventory 音频路径越界，拒绝删除: {path}") from exc
        if path.is_file():
            present_paths[index] = path
            directory_owners.setdefault(path.parent, set()).add(index)

    eligible_indexes = {
        index
        for index, song in enumerate(songs)
        if index in present_paths
        and song.get("download", {}).get("status") == "downloaded"
        and source_track_is_analyzed(song, source_ids)
    }
    retained_indexes = set(present_paths) - eligible_indexes
    already_purged_indexes = {
        index
        for index, song in enumerate(songs)
        if index not in present_paths
        and song.get("download", {}).get("retention") == "purged_after_analysis"
    }
    selected_directories = {
        directory
        for directory, owners in directory_owners.items()
        if owners and owners <= eligible_indexes and directory != root
    }
    selected_files = {
        present_paths[index]
        for index in eligible_indexes
        if present_paths[index].parent not in selected_directories
    }
    directory_files = {
        path
        for directory in selected_directories
        for path in directory.rglob("*")
        if path.is_file()
    }
    deletion_files = directory_files | selected_files
    bytes_total = sum(path.stat().st_size for path in deletion_files)
    summary: dict[str, Any] = {
        "inventory": str(inventory_path.resolve()),
        "audio_root": str(root),
        "knowledge_db": str(knowledge_db.resolve()),
        "song_count": len(songs),
        "eligible_song_count": len(eligible_indexes),
        "retained_song_count": len(retained_indexes),
        "already_purged_song_count": len(already_purged_indexes),
        "selected_directory_count": len(selected_directories),
        "selected_audio_file_count": len(eligible_indexes),
        "file_count": len(deletion_files),
        "bytes": bytes_total,
        "release_counts": release_counts,
        "dry_run": not confirm,
    }
    if not confirm:
        return summary

    for directory in sorted(selected_directories, key=lambda path: len(path.parts), reverse=True):
        shutil.rmtree(directory)
    for path in selected_files:
        path.unlink(missing_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    deleted_at = now_iso()
    for index in sorted(eligible_indexes):
        song = songs[index]
        download = song.setdefault("download", {})
        download.update(
            {
                "status": "downloaded",
                "retention": "purged_after_analysis",
                "file_present": False,
                "exists": False,
                "purged_at": deleted_at,
                "purged_reason": "audio no longer needed after knowledge-base import",
            }
        )
    inventory["generated_at"] = deleted_at
    inventory["audio_retention"] = (
        "partial_purge_after_analysis" if retained_indexes else "purged_after_analysis"
    )
    inventory["audio_files_deleted_at"] = deleted_at
    atomic_write_json(inventory_path, inventory)
    summary.update(
        {
            "dry_run": False,
            "deleted_at": deleted_at,
            "inventory_updated": True,
            "remaining_present_song_count": len(retained_indexes),
        }
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--knowledge-db", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--confirm-delete-audio", action="store_true")
    args = parser.parse_args()
    result = prune(
        args.inventory.expanduser(),
        args.audio_root.expanduser(),
        args.knowledge_db.expanduser(),
        args.expected_count,
        args.confirm_delete_audio,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

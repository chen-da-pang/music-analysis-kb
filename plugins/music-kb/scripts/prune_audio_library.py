#!/usr/bin/env python3
"""Delete local audio after the knowledge-base release is verified.

The inventory remains the deduplication authority. Purged records keep their
identity and historical path but are marked ``purged_after_analysis``; future
queue preparation therefore skips them without requiring the audio file.
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


def validate_release(knowledge_db: Path, expected_count: int) -> dict[str, int]:
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
    except sqlite3.Error as exc:
        raise RuntimeError(f"知识库缺少可验证的链接/来源表，不能删除音频: {exc}") from exc
    finally:
        connection.close()
    for key, value in counts.items():
        if int(value) < expected_count:
            raise RuntimeError(f"知识库校验不足: {key}={value}, expected>={expected_count}")
    return {key: int(value) for key, value in counts.items()}


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
    incomplete = [
        song.get("identity_key")
        for song in songs
        if song.get("download", {}).get("status") != "downloaded"
    ]
    if incomplete:
        raise RuntimeError(f"仍有 {len(incomplete)} 首歌曲未完成入库，不能删除音频")
    release_counts = validate_release(knowledge_db, expected_count)
    root = audio_root.expanduser().resolve()
    files = [path for path in root.rglob("*") if path.is_file()] if root.exists() else []
    bytes_total = sum(path.stat().st_size for path in files)
    summary: dict[str, Any] = {
        "inventory": str(inventory_path.resolve()),
        "audio_root": str(root),
        "knowledge_db": str(knowledge_db.resolve()),
        "song_count": len(songs),
        "file_count": len(files),
        "bytes": bytes_total,
        "release_counts": release_counts,
        "dry_run": not confirm,
    }
    if not confirm:
        return summary

    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    deleted_at = now_iso()
    for song in songs:
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
    inventory["audio_retention"] = "purged_after_analysis"
    inventory["audio_files_deleted_at"] = deleted_at
    atomic_write_json(inventory_path, inventory)
    summary.update({"dry_run": False, "deleted_at": deleted_at, "inventory_updated": True})
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

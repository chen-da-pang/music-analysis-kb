#!/usr/bin/env python3
"""Prepare a deduplicated weekly download queue from a Kugou chart export."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
import unicodedata
from collections import OrderedDict
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


def identity_key(platform: str, track_key: Any) -> str | None:
    if track_key is None or str(track_key).strip() == "":
        return None
    return f"{platform}:{str(track_key).strip()}"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_source(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    metadata: dict[str, Any] = {"source_path": str(path.resolve())}
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [row for row in rows if isinstance(row, dict)], metadata
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)], metadata
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        metadata.update({"source_run": data.get("summary", {})})
        songs = data.get("songs", data.get("items", []))
    else:
        songs = data
    return [row for row in songs if isinstance(row, dict)], metadata


def candidate_from_row(row: dict[str, Any], metadata: dict[str, Any], platform: str) -> dict[str, Any] | None:
    title = row.get("song_name") or row.get("title") or row.get("canonical_title")
    artist = row.get("artist_name") or row.get("artist") or row.get("canonical_artist")
    if not title or not artist:
        return None
    track = row.get("mix_song_id") or row.get("platform_track_key") or row.get("track_id")
    candidate = {
        "identity_key": identity_key(platform, track),
        "title_artist_key": title_artist_key(platform, title, artist),
        "platform": platform,
        "platform_track_key": str(track).strip() if track is not None and str(track).strip() else None,
        "title": str(title),
        "artist": str(artist),
        "play_link": row.get("play_link"),
        "chart_count": row.get("chart_count"),
        "chart_names": row.get("chart_names"),
        "chart_appearances": row.get("chart_appearances", []),
        "source_run": metadata.get("source_run", {}),
    }
    return candidate


def download_present(song: dict[str, Any], audio_root: Path) -> bool:
    download = song.get("download")
    if not isinstance(download, dict) or download.get("status") != "downloaded":
        return False
    if download.get("retention") == "purged_after_analysis":
        return True
    path = download.get("path")
    if not path:
        return False
    candidate = Path(str(path)).expanduser()
    if not candidate.is_absolute():
        candidate = audio_root / candidate
    return candidate.is_file()


def prepare_queue(
    source: Path,
    inventory_path: Path,
    output: Path,
    audio_root: Path,
    platform: str,
    *,
    retry_abandoned: bool = False,
) -> dict[str, Any]:
    rows, metadata = read_source(source)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    existing = [song for song in inventory.get("songs", []) if isinstance(song, dict)]
    by_identity = {song.get("identity_key"): song for song in existing if song.get("identity_key")}
    by_title = {song.get("title_artist_key"): song for song in existing if song.get("title_artist_key")}

    unique: OrderedDict[str, dict[str, Any]] = OrderedDict()
    duplicate_source = 0
    invalid = 0
    for row in rows:
        candidate = candidate_from_row(row, metadata, platform)
        if candidate is None:
            invalid += 1
            continue
        key = candidate["identity_key"] or candidate["title_artist_key"]
        if key in unique:
            duplicate_source += 1
            old = unique[key]
            if not old.get("chart_appearances") and candidate.get("chart_appearances"):
                old["chart_appearances"] = candidate["chart_appearances"]
            continue
        unique[key] = candidate

    queue: list[dict[str, Any]] = []
    skipped_existing = 0
    skipped_abandoned = 0
    redownload_missing = 0
    for candidate in unique.values():
        old = by_identity.get(candidate["identity_key"]) if candidate.get("identity_key") else None
        old = old or by_title.get(candidate["title_artist_key"])
        if old and download_present(old, audio_root):
            skipped_existing += 1
            continue
        if old and isinstance(old.get("download"), dict):
            old_status = old["download"].get("status")
            if old_status == "abandoned" and not retry_abandoned:
                skipped_abandoned += 1
                continue
            if old_status == "missing":
                redownload_missing += 1
        queue.append(candidate)

    payload = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in queue)
    atomic_write_text(output, payload)
    source_run = metadata.get("source_run") or {}
    manifest = {
        "schema_version": 1,
        "created_at": now_iso(),
        "source": str(source.resolve()),
        "source_run_id": source_run.get("run_id"),
        "inventory": str(inventory_path.resolve()),
        "audio_root": str(audio_root.resolve()),
        "platform": platform,
        "source_records": len(rows),
        "source_unique_records": len(unique),
        "duplicate_source_records": duplicate_source,
        "invalid_source_records": invalid,
        "skipped_existing_download": skipped_existing,
        "skipped_abandoned": skipped_abandoned,
        "redownload_missing": redownload_missing,
        "retry_abandoned": retry_abandoned,
        "queued": len(queue),
        "queue": str(output.resolve()),
    }
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--platform", default="kugou")
    parser.add_argument(
        "--retry-abandoned",
        action="store_true",
        help="Explicitly requeue records that reached the fallback retry limit.",
    )
    args = parser.parse_args()
    manifest = prepare_queue(
        args.source.expanduser(),
        args.inventory.expanduser(),
        args.output.expanduser(),
        args.audio_root.expanduser(),
        args.platform,
        retry_abandoned=args.retry_abandoned,
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

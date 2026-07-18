#!/usr/bin/env python3
"""Materialize the current weekly download queue as a hash-addressed CNB input."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from music_kb.operation_context import RunContext, atom, load_validated_operations

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("queue must contain only JSON objects")
    return rows


def resolve_audio(raw: Any, audio_root: Path) -> Path:
    path = Path(str(raw or "")).expanduser()
    return path if path.is_absolute() else audio_root / path


def materialize(queue_path: Path, inventory_path: Path, audio_root: Path, destination: Path, campaign_id: str) -> dict[str, Any]:
    if not SAFE_ID.fullmatch(campaign_id):
        raise ValueError(f"unsafe campaign id: {campaign_id!r}")
    queue = read_rows(queue_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    by_identity = {song.get("identity_key"): song for song in inventory.get("songs", []) if song.get("identity_key")}
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    hardlinked = copied = 0
    for index, candidate in enumerate(queue, start=1):
        identity = str(candidate.get("identity_key") or "").strip()
        if not identity or identity in seen:
            raise ValueError(f"queue contains missing or duplicate identity at row {index}: {identity!r}")
        seen.add(identity)
        song = by_identity.get(identity)
        if not song:
            raise ValueError(f"queue identity missing from inventory: {identity}")
        download = song.get("download", {})
        if download.get("status") != "downloaded":
            raise ValueError(f"song is not downloaded: {identity} ({download.get('status')})")
        source = resolve_audio(download.get("path"), audio_root)
        if not source.is_file():
            raise ValueError(f"audio file missing for {identity}: {source}")
        track = str(song.get("platform_track_key") or identity.split(":", 1)[-1])
        item_id = f"kugou-{track}"
        if not SAFE_ID.fullmatch(item_id):
            raise ValueError(f"unsafe item id: {item_id!r}")
        relative = Path("audio") / f"{item_id}{source.suffix.lower()}"
        staged = destination / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        if staged.exists():
            if staged.stat().st_size != source.stat().st_size or sha256_file(staged) != sha256_file(source):
                raise ValueError(f"existing staged file differs: {staged}")
        else:
            try:
                os.link(source, staged)
                hardlinked += 1
            except OSError:
                shutil.copy2(source, staged)
                copied += 1
        source_url = str(song.get("play_link") or "").strip()
        if source_url:
            parsed = urlsplit(source_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"unsafe play_link for {identity}: {source_url!r}")
        row = {"id": item_id, "relative_audio_path": relative.as_posix(), "source_bytes": source.stat().st_size, "sha256": sha256_file(source), "title": str(song.get("title") or "").strip(), "artist": str(song.get("artist") or "").strip(), "campaign_id": campaign_id}
        if source_url:
            row["source_url"] = source_url
        rows.append(row)
    atomic_write(destination / "manifest.jsonl", "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows))
    summary = {"campaign_id": campaign_id, "queue": str(queue_path.resolve()), "inventory": str(inventory_path.resolve()), "destination": str(destination.resolve()), "item_count": len(rows), "unique_identity_keys": len(seen), "source_links": sum(1 for row in rows if row.get("source_url")), "hardlinked": hardlinked, "copied": copied, "manifest": str((destination / "manifest.jsonl").resolve()), "created_at": now_iso()}
    atomic_write(destination / "materialization_summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--operations-file", type=Path, default=Path(__file__).resolve().parents[1] / "references" / "validated-operations.json")
    args = parser.parse_args()
    operations = args.operations_file.expanduser().resolve()
    load_validated_operations(operations, required_atom="cnb_input_materialization")
    summary = materialize(args.queue.expanduser(), args.inventory.expanduser(), args.audio_root.expanduser(), args.destination.expanduser(), args.campaign_id)
    with RunContext(run_id=args.run_id, run_dir=args.workspace.expanduser().resolve() / "data" / "weekly_runs" / args.run_id, operations_file=operations) as context:
        with atom(context, "cnb_input_materialization", inputs={"queue": str(args.queue), "inventory": str(args.inventory), "destination": str(args.destination)}) as outputs:
            outputs.update(summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

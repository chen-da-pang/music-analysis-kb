#!/usr/bin/env python3
"""Prepare an alternate-source recovery queue from explicitly retryable inventory rows."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_RETRY_STATUSES = {"no_results", "failed"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def parse_statuses(value: str) -> list[str]:
    statuses = [status.strip() for status in value.split(",") if status.strip()]
    if not statuses:
        raise ValueError("--statuses must name at least one retryable status")
    invalid = sorted(set(statuses) - ALLOWED_RETRY_STATUSES)
    if invalid:
        raise ValueError(f"unsupported retry statuses: {', '.join(invalid)}")
    return list(dict.fromkeys(statuses))


def prepare(inventory_path: Path, output: Path, profile_path: Path, statuses: list[str]) -> dict[str, Any]:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    aliases = profile.get("artist_aliases", {})
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    status_counts = {status: 0 for status in statuses}
    for song in inventory.get("songs", []):
        if not isinstance(song, dict):
            continue
        status = song.get("download", {}).get("status")
        if status not in statuses:
            continue
        identity = song.get("identity_key") or song.get("title_artist_key")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        row = {key: song.get(key) for key in ("identity_key", "title_artist_key", "platform", "platform_track_key", "title", "artist", "play_link")}
        row["artist_aliases"] = aliases.get(f"{song.get('title')}\u0000{song.get('artist')}", [])
        row["retry_from_status"] = status
        rows.append(row)
        status_counts[status] += 1
    atomic_write(output, "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows))
    return {
        "schema_version": 1,
        "created_at": now_iso(),
        "inventory": str(inventory_path.resolve()),
        "profile": str(profile_path.resolve()),
        "retry_statuses": statuses,
        "status_counts": status_counts,
        "queued": len(rows),
        "unique_identity_keys": len(seen),
        "queue": str(output.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--statuses", required=True, help="Comma-separated retryable statuses: no_results,failed")
    args = parser.parse_args()
    print(json.dumps(prepare(args.inventory.expanduser(), args.output.expanduser(), args.profile.expanduser(), parse_statuses(args.statuses)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

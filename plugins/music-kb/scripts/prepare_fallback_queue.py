#!/usr/bin/env python3
"""Prepare a fallback queue from inventory records currently marked no_results."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def prepare(inventory_path: Path, output: Path, profile_path: Path) -> dict[str, Any]:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    aliases = profile.get("artist_aliases", {})
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for song in inventory.get("songs", []):
        if not isinstance(song, dict) or song.get("download", {}).get("status") != "no_results":
            continue
        identity = song.get("identity_key") or song.get("title_artist_key")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        row = {key: song.get(key) for key in ("identity_key", "title_artist_key", "platform", "platform_track_key", "title", "artist", "play_link")}
        row["artist_aliases"] = aliases.get(f"{song.get('title')}\u0000{song.get('artist')}", [])
        rows.append(row)
    atomic_write(output, "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows))
    return {"schema_version": 1, "created_at": now_iso(), "inventory": str(inventory_path.resolve()), "profile": str(profile_path.resolve()), "queued": len(rows), "unique_identity_keys": len(seen), "queue": str(output.resolve())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.inventory.expanduser(), args.output.expanduser(), args.profile.expanduser()), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

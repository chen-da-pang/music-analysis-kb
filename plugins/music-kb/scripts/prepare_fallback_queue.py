#!/usr/bin/env python3
"""Prepare an alternate-source recovery queue from explicitly retryable inventory rows."""

from __future__ import annotations

import argparse
import hashlib
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


def load_source_queue_scope(source_queue: Path | None) -> dict[str, Any] | None:
    """Load a primary JSONL queue as an exact, duplicate-free identity scope."""

    if source_queue is None:
        return None
    if not source_queue.is_file():
        raise FileNotFoundError(f"source queue does not exist: {source_queue}")
    raw = source_queue.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"source queue is not UTF-8: {source_queue}") from exc

    identities: set[str] = set()
    identity_lines: dict[str, int] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"source queue line {line_number} is not valid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"source queue line {line_number} must be a JSON object")
        identity = row.get("identity_key") or row.get("platform_track_key")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError(f"source queue line {line_number} has no non-empty identity_key/platform_track_key")
        identity = identity.strip()
        previous_line = identity_lines.get(identity)
        if previous_line is not None:
            raise ValueError(
                f"source queue repeats identity {identity!r} on lines {previous_line} and {line_number}"
            )
        identity_lines[identity] = line_number
        identities.add(identity)
    if not identities:
        raise ValueError("source queue contains no identities")
    return {
        "path": str(source_queue.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "identities": identities,
    }


def prepare(
    inventory_path: Path,
    output: Path,
    profile_path: Path,
    statuses: list[str],
    source_queue: Path | None = None,
) -> dict[str, Any]:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    aliases = profile.get("artist_aliases", {})
    source_scope = load_source_queue_scope(source_queue)
    source_identities = source_scope["identities"] if source_scope is not None else None
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    status_counts = {status: 0 for status in statuses}
    abandoned_excluded = 0
    for song in inventory.get("songs", []):
        if not isinstance(song, dict):
            continue
        download = song.get("download") if isinstance(song.get("download"), dict) else {}
        status = download.get("status")
        identity = song.get("identity_key") or song.get("title_artist_key")
        if source_identities is not None and identity not in source_identities:
            continue
        if status == "abandoned":
            abandoned_excluded += 1
            continue
        if status not in statuses:
            continue
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
        "abandoned_excluded": abandoned_excluded,
        "source_queue": source_scope["path"] if source_scope is not None else None,
        "source_queue_sha256": source_scope["sha256"] if source_scope is not None else None,
        "source_queue_identity_keys": len(source_identities) if source_identities is not None else None,
        "source_queue_unqueued_identity_keys": len(source_identities - seen) if source_identities is not None else None,
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
    parser.add_argument(
        "--source-queue",
        type=Path,
        help="Optional primary JSONL queue; fallback may include only its unique identities",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(
                args.inventory.expanduser(),
                args.output.expanduser(),
                args.profile.expanduser(),
                parse_statuses(args.statuses),
                args.source_queue.expanduser().resolve() if args.source_queue else None,
            ),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

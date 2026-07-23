"""Publisher helpers for materializing the no-audio CC lyric backfill queue."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .repository import MusicKBRepository


_ARCHIVED_KUGOU_FILE_HASH = re.compile(
    r" - ([0-9a-f]{32})\.[a-z0-9]{2,5}$", re.IGNORECASE
)


def _atomic_write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archived_kugou_hashes_by_identity(
    inventory: str | Path | None,
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """Recover exact audio hashes retained in the durable download inventory.

    A purged audio file cannot be re-read, but its original ``musicdl`` path
    retains the Kugou file hash.  This bridge is valid only when the inventory
    row itself is keyed by the same exact ``kugou:<MixSongID>`` identity and
    records a completed download.  It is never derived from title or artist.
    """

    if inventory is None:
        return {}, {"status": "not_supplied", "attached": 0, "conflicts": 0}
    path = Path(inventory).expanduser().resolve()
    if not path.is_file():
        return {}, {
            "status": "not_found",
            "inventory": str(path),
            "attached": 0,
            "conflicts": 0,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"song inventory is not valid JSON: {path}") from exc
    songs = payload.get("songs") if isinstance(payload, Mapping) else None
    if not isinstance(songs, list):
        raise ValueError(f"song inventory has no songs list: {path}")

    hashes: dict[str, dict[str, str]] = {}
    conflicts: set[str] = set()
    for song in songs:
        if not isinstance(song, Mapping):
            continue
        identity = str(song.get("identity_key") or "").strip()
        download = song.get("download")
        if not identity.startswith("kugou:") or not isinstance(download, Mapping):
            continue
        if str(download.get("status") or "").strip() != "downloaded":
            continue
        relative_path = str(download.get("path") or "").strip()
        match = _ARCHIVED_KUGOU_FILE_HASH.search(relative_path)
        if match is None:
            continue
        value = {
            "file_hash": match.group(1).upper(),
            "relative_path": relative_path,
            "retention": str(download.get("retention") or "retained"),
        }
        previous = hashes.get(identity)
        if previous is not None and previous["file_hash"] != value["file_hash"]:
            hashes.pop(identity, None)
            conflicts.add(identity)
            continue
        if identity not in conflicts:
            hashes[identity] = value
    return hashes, {
        "status": "loaded",
        "inventory": str(path),
        "inventory_sha256": _sha256_file(path),
        "available": len(hashes),
        "attached": 0,
        "conflicts": len(conflicts),
        "conflict_identities": sorted(conflicts)[:20],
    }


def _attach_archived_kugou_hashes(
    rows: Sequence[Mapping[str, Any]], inventory: str | Path | None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    hashes, manifest = _archived_kugou_hashes_by_identity(inventory)
    attached = 0
    enriched: list[dict[str, Any]] = []
    for raw_row in rows:
        row = dict(raw_row)
        identity = str(row.get("identity_key") or "").strip()
        proof = hashes.get(identity)
        if proof is not None:
            row["archived_kugou_file_hash"] = proof["file_hash"]
            row["archived_kugou_file_hash_provenance"] = {
                "method": "song_inventory_download_path_exact_identity_v1",
                "inventory_identity_key": identity,
                "download_status": "downloaded",
                "download_retention": proof["retention"],
                "inventory_relative_audio_path": proof["relative_path"],
            }
            attached += 1
        enriched.append(row)
    return enriched, {**manifest, "attached": attached}


def materialize_lyric_backfill_queue(
    database: str | Path,
    output: str | Path,
    *,
    unresolved_only: bool = True,
    chart_database: str | Path | None = None,
    inventory: str | Path | None = None,
) -> dict[str, Any]:
    """Write an immutable-input JSONL queue from the publisher master.

    The generated file is operational data and must remain outside the plugin
    repository.  It carries the canonical recording/source assertions so the
    worker receipt can be rejected if it drifts before import.
    """

    destination = Path(output).expanduser().resolve()
    with MusicKBRepository(database, read_only=True) as repository:
        plan = repository.prepare_lyric_backfill_queue(
            unresolved_only=unresolved_only,
            chart_database=chart_database,
        )
    rows = plan.pop("rows")
    assert isinstance(rows, list)
    rows, archived_hash_bridge = _attach_archived_kugou_hashes(rows, inventory)
    _atomic_write_jsonl(destination, rows)
    resolved_chart_database = plan.get("chart_database")
    chart_sha256 = (
        _sha256_file(Path(str(resolved_chart_database)))
        if resolved_chart_database
        else None
    )
    return {
        **plan,
        "queue": str(destination),
        "queue_bytes": destination.stat().st_size,
        "queue_sha256": _sha256_file(destination),
        "chart_database_sha256": chart_sha256,
        "archived_hash_bridge": archived_hash_bridge,
    }

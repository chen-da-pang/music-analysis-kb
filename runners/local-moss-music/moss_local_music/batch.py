"""Batch analysis over a download queue: resume, failure isolation, delivery.

Items join the download queue to the song inventory (status ``downloaded``
with the audio file present on disk); items without audio are skipped with an
audit event, failed analyses are isolated, and completed items are never
re-analyzed on resume — the per-song sidecar is the completion record.
Delivery rows are rebuilt from every completed sidecar in queue order, so a
partial batch still yields a valid canonical delivery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .delivery import TrackIdentity, build_delivery_row, write_delivery_jsonl
from .runner import run_single

EVENTS_NAME = "events.jsonl"


@dataclass(frozen=True)
class BatchItem:
    item_id: str
    title: str
    artist: str
    audio_path: Path
    relative_audio_path: str
    source_url: str | None


def load_batch_items(
    queue_path: str | Path,
    inventory_path: str | Path,
    audio_root: str | Path,
) -> tuple[list[BatchItem], list[tuple[str, str]]]:
    """Join queue rows to inventory download records that exist on disk.

    Returns (audio-ready items, exclusions) where each exclusion is an
    ``(identity_key, reason)`` pair for audit events."""

    inventory = json.loads(Path(inventory_path).read_text(encoding="utf-8"))
    downloads = {}
    for song in inventory.get("songs", []):
        download = song.get("download") or {}
        if download.get("status") == "downloaded" and download.get("path"):
            downloads[str(song.get("identity_key"))] = str(download["path"])
    items: list[BatchItem] = []
    excluded: list[tuple[str, str]] = []
    with Path(queue_path).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            identity = str(row.get("identity_key"))
            relative = downloads.get(identity)
            if relative is None:
                excluded.append((identity, "not_downloaded"))
                continue
            audio = Path(audio_root) / relative
            if not audio.is_file():
                excluded.append((identity, "audio_file_missing"))
                continue
            items.append(
                BatchItem(
                    item_id=str(row["platform_track_key"]),
                    title=str(row.get("title") or ""),
                    artist=str(row.get("artist") or ""),
                    audio_path=audio,
                    relative_audio_path=relative,
                    source_url=row.get("play_link"),
                )
            )
    return items, excluded


def _append_event(events_path: Path, run_id: str, event: str, item_id: str, **detail: Any) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "run_id": run_id,
        "event": event,
        "item_id": item_id,
    }
    record.update(detail)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _sidecar_for(analysis_dir: Path, item_id: str) -> Optional[dict[str, Any]]:
    path = analysis_dir / f"{item_id}.sidecar.json"
    if not path.is_file():
        return None
    try:
        sidecar = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not sidecar.get("answer"):
        return None
    return sidecar


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def run_batch(
    queue_path: str | Path,
    *,
    inventory_path: str | Path,
    audio_root: str | Path,
    analysis_dir: str | Path,
    delivery_path: str | Path,
    run_id: str,
    generate_fn: Optional[Callable[..., tuple[str, int, float]]] = None,
    model_path: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    analysis = Path(analysis_dir)
    analysis.mkdir(parents=True, exist_ok=True)
    events_path = analysis / EVENTS_NAME
    queue_rows = sum(1 for line in Path(queue_path).open(encoding="utf-8") if line.strip())
    items, excluded = load_batch_items(queue_path, inventory_path, audio_root)
    for identity, reason in excluded:
        _append_event(events_path, run_id, "skipped_no_audio", identity, reason=reason)
    if limit is not None:
        items = items[:limit]

    completed: dict[str, dict[str, Any]] = {}
    resumed = attempted = failed = 0
    for item in items:
        sidecar = _sidecar_for(analysis, item.item_id)
        if sidecar is not None:
            completed[item.item_id] = sidecar
            resumed += 1
            continue
        _append_event(events_path, run_id, "started", item.item_id, audio=str(item.audio_path))
        attempted += 1
        try:
            sidecar = run_single(
                str(item.audio_path),
                analysis,
                model_path=model_path,
                run_id=run_id,
                item_id=item.item_id,
                generate_fn=generate_fn,
            )
        except Exception as exc:  # isolation: one bad song never stops the batch
            _append_event(events_path, run_id, "failed", item.item_id, error=f"{type(exc).__name__}: {exc}")
            failed += 1
            continue
        completed[item.item_id] = sidecar
        _append_event(
            events_path,
            run_id,
            "completed",
            item.item_id,
            generated_token_count=sidecar["generated_token_count"],
            elapsed_seconds=sidecar["elapsed_seconds"],
        )

    rows = []
    delivered_items = [item for item in items if item.item_id in completed]
    for index, item in enumerate(delivered_items):
        sidecar = completed[item.item_id]
        audio_sha256, audio_bytes = _hash_file(item.audio_path)
        rows.append(
            build_delivery_row(
                sidecar,
                TrackIdentity(
                    kugou_mix_song_id=item.item_id,
                    title=item.title,
                    artist=item.artist,
                    relative_audio_path=item.relative_audio_path,
                    source_url=item.source_url,
                ),
                campaign_id=run_id,
                manifest_index=index,
                audio_sha256=audio_sha256,
                audio_bytes=audio_bytes,
            )
        )
    delivery = None
    if rows:
        delivery = write_delivery_jsonl(rows, delivery_path)
    return {
        "run_id": run_id,
        "queue_rows": queue_rows,
        "items": len(items),
        "skipped_no_audio": len(excluded),
        "completed": len(completed),
        "resumed": resumed,
        "attempted": attempted,
        "failed": failed,
        "delivered": len(rows),
        "delivery_path": str(delivery) if delivery else None,
        "events_path": str(events_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, help="download queue JSONL")
    parser.add_argument("--inventory", required=True, help="song inventory JSON")
    parser.add_argument("--audio-root", required=True)
    parser.add_argument("--analysis-dir", required=True)
    parser.add_argument("--delivery-path", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default=None, help="converted MLX model dir (defaults to the HF cache snapshot)")
    parser.add_argument("--limit", type=int, default=None, help="analyze at most the first N audio-ready items")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")

    summary = run_batch(
        args.queue,
        inventory_path=args.inventory,
        audio_root=args.audio_root,
        analysis_dir=args.analysis_dir,
        delivery_path=args.delivery_path,
        run_id=args.run_id,
        model_path=args.model,
        limit=args.limit,
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

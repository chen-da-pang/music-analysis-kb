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


def identity_sha256(identities: list[str]) -> str:
    return hashlib.sha256(
        "".join(f"{identity}\n" for identity in identities).encode("utf-8")
    ).hexdigest()


def _validated_partial_selection(
    *,
    expected_source_count: int | None,
    expected_selected_count: int | None,
    expected_excluded_status_counts: dict[str, int] | None,
) -> tuple[bool, dict[str, int]]:
    """Validate the explicit contract for a partial source queue selection."""

    requested = (
        expected_source_count is not None
        or expected_selected_count is not None
        or expected_excluded_status_counts is not None
    )
    if not requested:
        return False, {}
    if (
        isinstance(expected_source_count, bool)
        or not isinstance(expected_source_count, int)
        or expected_source_count < 1
        or isinstance(expected_selected_count, bool)
        or not isinstance(expected_selected_count, int)
        or expected_selected_count < 1
        or not isinstance(expected_excluded_status_counts, dict)
        or not expected_excluded_status_counts
    ):
        raise ValueError(
            "partial selection requires positive expected source/selected counts and explicit excluded status counts"
        )
    normalized: dict[str, int] = {}
    for raw_status, raw_count in expected_excluded_status_counts.items():
        status = str(raw_status).strip()
        if not re.fullmatch(r"[a-z_]+", status):
            raise ValueError(f"invalid excluded download status: {raw_status!r}")
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 1
        ):
            raise ValueError(f"invalid expected excluded count for {status}: {raw_count!r}")
        if status == "downloaded":
            raise ValueError("partial selection cannot exclude downloaded rows")
        normalized[status] = raw_count
    if expected_source_count != expected_selected_count + sum(normalized.values()):
        raise ValueError(
            "partial selection expected source count must equal selected plus explicit excluded counts"
        )
    return True, normalized


def materialize(
    queue_path: Path,
    inventory_path: Path,
    audio_root: Path,
    destination: Path,
    campaign_id: str,
    *,
    expected_source_count: int | None = None,
    expected_selected_count: int | None = None,
    expected_excluded_status_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    if not SAFE_ID.fullmatch(campaign_id):
        raise ValueError(f"unsafe campaign id: {campaign_id!r}")
    partial_selection, expected_excluded = _validated_partial_selection(
        expected_source_count=expected_source_count,
        expected_selected_count=expected_selected_count,
        expected_excluded_status_counts=expected_excluded_status_counts,
    )
    queue = read_rows(queue_path)
    if partial_selection and len(queue) != expected_source_count:
        raise ValueError(
            f"source queue count does not match partial selection contract: {len(queue)} != {expected_source_count}"
        )
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    by_identity = {song.get("identity_key"): song for song in inventory.get("songs", []) if song.get("identity_key")}
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    selected_identities: list[str] = []
    excluded_identities: list[dict[str, str]] = []
    excluded_by_status: dict[str, int] = {}
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
        status = str(download.get("status") or "").strip()
        if status != "downloaded":
            if not partial_selection:
                raise ValueError(f"song is not downloaded: {identity} ({status})")
            if status not in expected_excluded:
                raise ValueError(
                    f"partial selection encountered an unapproved download status: {identity} ({status})"
                )
            excluded_by_status[status] = excluded_by_status.get(status, 0) + 1
            excluded_identities.append({"identity_key": identity, "download_status": status})
            continue
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
        selected_identities.append(identity)
    if partial_selection:
        if len(rows) != expected_selected_count:
            raise ValueError(
                f"selected downloaded count does not match partial selection contract: {len(rows)} != {expected_selected_count}"
            )
        if excluded_by_status != expected_excluded:
            raise ValueError(
                "excluded download statuses do not match partial selection contract: "
                f"{excluded_by_status} != {expected_excluded}"
            )
    atomic_write(destination / "manifest.jsonl", "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows))
    manifest_path = destination / "manifest.jsonl"
    selection_receipt_path = destination / "selection-receipt.json"
    selection_receipt = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "selection_mode": (
            "downloaded_with_explicit_terminal_exclusions"
            if partial_selection
            else "strict_all_downloaded"
        ),
        "source_queue": str(queue_path.resolve()),
        "source_queue_sha256": sha256_file(queue_path),
        "source_queue_item_count": len(queue),
        "inventory": str(inventory_path.resolve()),
        "inventory_sha256": sha256_file(inventory_path),
        "selected_manifest": str(manifest_path.resolve()),
        "selected_manifest_sha256": sha256_file(manifest_path),
        "selected_item_count": len(rows),
        "selected_identity_sha256": identity_sha256(selected_identities),
        "excluded_by_download_status": excluded_by_status,
        "excluded_identity_sha256": identity_sha256(
            [item["identity_key"] for item in excluded_identities]
        ),
        "excluded_identities": excluded_identities,
        "expected_source_count": expected_source_count,
        "expected_selected_count": expected_selected_count,
        "expected_excluded_status_counts": expected_excluded if partial_selection else {},
        "created_at": now_iso(),
    }
    atomic_write(
        selection_receipt_path,
        json.dumps(selection_receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    summary = {
        "campaign_id": campaign_id,
        "queue": str(queue_path.resolve()),
        "inventory": str(inventory_path.resolve()),
        "destination": str(destination.resolve()),
        "item_count": len(rows),
        "unique_identity_keys": len(seen),
        "source_links": sum(1 for row in rows if row.get("source_url")),
        "hardlinked": hardlinked,
        "copied": copied,
        "manifest": str(manifest_path.resolve()),
        "selection_receipt": str(selection_receipt_path.resolve()),
        "selection_receipt_sha256": sha256_file(selection_receipt_path),
        "source_queue_item_count": len(queue),
        "excluded_by_download_status": excluded_by_status,
        "created_at": now_iso(),
    }
    atomic_write(destination / "materialization_summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def parse_excluded_status_counts(values: list[str]) -> dict[str, int] | None:
    if not values:
        return None
    result: dict[str, int] = {}
    for value in values:
        status, separator, raw_count = value.partition("=")
        if not separator:
            raise ValueError("--expected-excluded-status must use STATUS=COUNT")
        normalized_status = status.strip()
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise ValueError(f"invalid --expected-excluded-status count: {value!r}") from exc
        if normalized_status in result:
            raise ValueError(f"duplicate --expected-excluded-status: {normalized_status}")
        result[normalized_status] = count
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--expected-source-count",
        type=int,
        help="Required with partial downloaded-only selection; exact source queue row count",
    )
    parser.add_argument(
        "--expected-selected-count",
        type=int,
        help="Required with partial downloaded-only selection; exact downloaded row count",
    )
    parser.add_argument(
        "--expected-excluded-status",
        action="append",
        default=[],
        metavar="STATUS=COUNT",
        help="Explicit terminal download status and count allowed to be excluded; repeatable",
    )
    parser.add_argument("--operations-file", type=Path, default=Path(__file__).resolve().parents[1] / "references" / "validated-operations.json")
    args = parser.parse_args()
    operations = args.operations_file.expanduser().resolve()
    load_validated_operations(operations, required_atom="cnb_input_materialization")
    expected_excluded_status_counts = parse_excluded_status_counts(
        args.expected_excluded_status
    )
    summary = materialize(
        args.queue.expanduser(),
        args.inventory.expanduser(),
        args.audio_root.expanduser(),
        args.destination.expanduser(),
        args.campaign_id,
        expected_source_count=args.expected_source_count,
        expected_selected_count=args.expected_selected_count,
        expected_excluded_status_counts=expected_excluded_status_counts,
    )
    with RunContext(run_id=args.run_id, run_dir=args.workspace.expanduser().resolve() / "data" / "weekly_runs" / args.run_id, operations_file=operations) as context:
        with atom(context, "cnb_input_materialization", inputs={
            "queue": str(args.queue),
            "inventory": str(args.inventory),
            "destination": str(args.destination),
            "expected_source_count": args.expected_source_count,
            "expected_selected_count": args.expected_selected_count,
            "expected_excluded_status_counts": expected_excluded_status_counts,
        }) as outputs:
            outputs.update(summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

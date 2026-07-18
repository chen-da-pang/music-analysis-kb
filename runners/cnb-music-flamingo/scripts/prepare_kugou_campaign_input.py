#!/usr/bin/env python3
"""Materialize the authoritative KuGou download manifest as campaign input."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EXTENSION_RE = re.compile(r"^[A-Za-z0-9]{1,12}$")


class CampaignInputError(ValueError):
    """Raised when a local download manifest cannot form a safe campaign."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_item_id(value: object) -> str:
    item_id = str(value or "").strip()
    if not _ITEM_ID_RE.fullmatch(item_id):
        raise CampaignInputError(f"Unsafe campaign item id: {value!r}")
    return item_id


def _extension(record: dict[str, Any], source: Path) -> str:
    value = str(record.get("ext") or source.suffix.lstrip(".")).strip().lstrip(".").lower()
    if not _EXTENSION_RE.fullmatch(value):
        raise CampaignInputError(f"Unsafe extension for {source}: {value!r}")
    actual_extension = source.suffix.lower().lstrip(".")
    # One verified KuGou download has no filename extension even though the
    # source manifest records it as FLAC. The manifest remains authoritative
    # for extensionless files; a conflicting explicit suffix is still unsafe.
    if actual_extension and actual_extension != value:
        raise CampaignInputError(
            f"Manifest extension {value!r} does not match source filename {source.name!r}"
        )
    return value


def _load_downloaded(source_progress: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(source_progress.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignInputError(f"Unable to read {source_progress}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CampaignInputError("download progress must be a JSON object")
    downloaded = payload.get("downloaded")
    if not isinstance(downloaded, dict) or not downloaded:
        raise CampaignInputError("downloaded must be a non-empty object")
    total = payload.get("total")
    if total is not None and (not isinstance(total, int) or total < len(downloaded)):
        raise CampaignInputError(
            f"download progress total={total!r} is smaller than downloaded count={len(downloaded)}"
        )
    if any(not isinstance(record, dict) for record in downloaded.values()):
        raise CampaignInputError("every downloaded entry must be an object")
    return downloaded


def _stage_file(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.stat().st_size != source.stat().st_size:
            raise CampaignInputError(f"Existing staged file has different size: {destination}")
        if sha256_file(destination) != sha256_file(source):
            raise CampaignInputError(f"Existing staged file has different digest: {destination}")
        return "reused"
    try:
        os.link(source, destination)
        return "hardlink"
    except OSError:
        shutil.copy2(source, destination)
        return "copy"


def _atomic_write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".manifest-",
        suffix=".tmp",
        delete=False,
    ) as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def materialize_campaign(source_progress: Path, destination: Path, campaign_id: str) -> dict[str, object]:
    """Create deterministic, safe, LFS-ready campaign input from local downloads."""
    campaign_id = _require_item_id(campaign_id)
    downloaded = _load_downloaded(source_progress)
    rows: list[dict[str, object]] = []
    transfer_counts = {"hardlink": 0, "copy": 0, "reused": 0}

    for raw_id in sorted(downloaded):
        item_id = _require_item_id(raw_id)
        record = downloaded[raw_id]
        source = Path(str(record.get("file") or "")).expanduser()
        if not source.is_file():
            raise CampaignInputError(f"Missing source audio for {item_id}: {source}")
        extension = _extension(record, source)
        relative = Path("audio") / f"{item_id}.{extension}"
        staged = destination / relative
        transfer_counts[_stage_file(source, staged)] += 1
        row = {
            "id": item_id,
            "relative_audio_path": relative.as_posix(),
            "source_bytes": source.stat().st_size,
            "sha256": sha256_file(source),
            "title": str(record.get("title") or "").strip(),
            "artist": str(record.get("artist") or "").strip(),
            "campaign_id": campaign_id,
        }
        source_url = str(record.get("play_link") or "").strip()
        if source_url:
            parsed = urlsplit(source_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise CampaignInputError(f"Unsafe source_url for {item_id}: {source_url!r}")
            row["source_url"] = source_url
        rows.append(row)

    _atomic_write_jsonl(destination / "manifest.jsonl", rows)
    summary = {
        "campaign_id": campaign_id,
        "item_count": len(rows),
        "manifest_path": str(destination / "manifest.jsonl"),
        "hardlinked": transfer_counts["hardlink"],
        "copied": transfer_counts["copy"],
        "reused": transfer_counts["reused"],
    }
    _atomic_write_jsonl(destination / "materialization_summary.jsonl", [summary])
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-progress", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    args = parser.parse_args()
    result = materialize_campaign(args.source_progress, args.destination, args.campaign_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

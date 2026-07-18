#!/usr/bin/env python3
"""Capture and normalize one Kugou chart page for a weekly run."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from music_kb.operation_context import atomic_write_json, load_validated_operations


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(text.split())


def safe_run_id(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if not value or any(character not in allowed for character in value):
        raise ValueError("run_id contains unsafe characters")
    return value


def _candidate(row: dict[str, Any], *, platform: str, rank: int) -> dict[str, Any] | None:
    title = row.get("song_name") or row.get("title") or row.get("canonical_title")
    artist = row.get("artist_name") or row.get("artist") or row.get("canonical_artist")
    if not title or not artist:
        return None
    track = row.get("mix_song_id") or row.get("platform_track_key") or row.get("track_id")
    track_text = str(track).strip() if track is not None and str(track).strip() else None
    return {
        "identity_key": f"{platform}:{track_text}" if track_text else None,
        "title_artist_key": f"{platform}:{normalize(title)}\x00{normalize(artist)}",
        "platform": platform,
        "platform_track_key": track_text,
        "title": str(title).strip(),
        "artist": str(artist).strip(),
        "play_link": row.get("play_link"),
        "chart_appearances": [{"rank": rank}],
    }


def normalize_payload(
    payload: dict[str, Any],
    *,
    platform: str = "kugou",
    rank_start: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if rank_start < 1:
        raise ValueError("rank_start must be positive")
    if payload.get("errcode") not in (None, 0):
        raise RuntimeError(f"kugou-cli returned errcode={payload.get('errcode')}: {payload.get('errmsg', '')}")
    data = payload.get("data")
    rows = data.get("list", []) if isinstance(data, dict) else []
    if not isinstance(rows, list):
        raise RuntimeError("kugou-cli response data.list is not an array")

    unique: OrderedDict[str, dict[str, Any]] = OrderedDict()
    invalid = 0
    duplicate = 0
    for rank, row in enumerate(rows, start=rank_start):
        if not isinstance(row, dict):
            invalid += 1
            continue
        candidate = _candidate(row, platform=platform, rank=rank)
        if candidate is None:
            invalid += 1
            continue
        key = candidate["identity_key"] or candidate["title_artist_key"]
        if key in unique:
            duplicate += 1
            unique[key]["chart_appearances"].extend(candidate["chart_appearances"])
            if not unique[key].get("play_link") and candidate.get("play_link"):
                unique[key]["play_link"] = candidate["play_link"]
            continue
        unique[key] = candidate
    return list(unique.values()), {
        "source_records": len(rows),
        "source_unique_records": len(unique),
        "duplicate_source_records": duplicate,
        "invalid_source_records": invalid,
    }


def capture_chart(
    *,
    run_id: str,
    rank_id: str,
    page: int,
    size: int,
    output_dir: Path,
    kugou_bin: str = "kugou-cli",
    timeout_seconds: int = 120,
    proxy: str | None = None,
    dry_run: bool = False,
    rank_start: int | None = None,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    safe_run_id(run_id)
    if not rank_id.isdigit() or int(rank_id) <= 0:
        raise ValueError("rank_id must be a positive integer")
    if page < 1 or size < 1 or size > 500:
        raise ValueError("page must be positive and size must be between 1 and 500")
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        kugou_bin,
        "--no-update-check",
        "music",
        "charts",
        rank_id,
        "--page",
        str(page),
        "--size",
        str(size),
    ]
    effective_rank_start = rank_start if rank_start is not None else (page - 1) * size + 1
    raw_path = output_dir / f"chart-{rank_id}-p{page}-raw.json"
    songs_path = output_dir / f"chart-{rank_id}-p{page}-songs.json"
    manifest_path = output_dir / f"chart-{rank_id}-p{page}-manifest.json"
    if dry_run:
        manifest = {
            "run_id": run_id,
            "rank_id": rank_id,
            "page": page,
            "size": size,
            "rank_start": effective_rank_start,
            "command": command,
            "dry_run": True,
            "raw": str(raw_path),
            "songs": str(songs_path),
        }
        atomic_write_json(manifest_path, manifest)
        return manifest

    env = os.environ.copy()
    if proxy:
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    result = runner(command, text=True, capture_output=True, timeout=timeout_seconds, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"kugou-cli failed with exit={result.returncode}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"kugou-cli returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("kugou-cli JSON root must be an object")
    songs, counts = normalize_payload(payload, rank_start=effective_rank_start)
    captured_at = now_iso()
    summary = {
        "run_id": run_id,
        "rank_id": rank_id,
        "page": page,
        "size": size,
        "rank_start": effective_rank_start,
        "rank_end": effective_rank_start + len(payload.get("data", {}).get("list", [])) - 1,
        "captured_at": captured_at,
        **counts,
    }
    atomic_write_json(raw_path, payload)
    atomic_write_json(
        songs_path,
        {
            "schema_version": 1,
            "platform": "kugou",
            "run_id": run_id,
            "summary": summary,
            "songs": songs,
        },
    )
    manifest = {
        **summary,
        "command": command,
        "dry_run": False,
        "raw": str(raw_path),
        "songs": str(songs_path),
        "manifest": str(manifest_path),
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--rank-id", required=True)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--kugou-bin", default="kugou-cli")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--proxy")
    parser.add_argument("--operations-file", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    load_validated_operations(args.operations_file, required_atom="chart_capture")
    result = capture_chart(
        run_id=args.run_id,
        rank_id=args.rank_id,
        page=args.page,
        size=args.size,
        output_dir=args.output_dir,
        kugou_bin=args.kugou_bin,
        timeout_seconds=args.timeout_seconds,
        proxy=args.proxy,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

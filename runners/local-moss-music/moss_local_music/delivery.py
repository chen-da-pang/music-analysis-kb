"""Canonical delivery adapter for local MOSS-Music analyses.

Maps one analysis sidecar plus its chart identity into a canonical delivery
row (16 required fields) accepted by the plugin's
``load_campaign_delivery_file`` validator, and writes strict LF JSONL.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTRACT = "local-moss-music-v1"
CANONICAL_SOURCE = "local-moss-music"


@dataclass(frozen=True)
class TrackIdentity:
    """Chart-sourced identity for one analyzed track.

    ``title``/``artist`` always come from chart metadata, never from the
    model text: the Thinking model demonstrably mislabels songs (T1 probe).
    """

    kugou_mix_song_id: str
    title: str
    artist: str
    relative_audio_path: str
    source_url: str | None = None


def build_delivery_row(
    sidecar: dict[str, Any],
    track: TrackIdentity,
    *,
    campaign_id: str,
    manifest_index: int,
    audio_sha256: str,
    audio_bytes: int,
) -> dict[str, Any]:
    answer = str(sidecar["answer"])
    row: dict[str, Any] = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "id": f"kugou-{track.kugou_mix_song_id}",
        "manifest_index": manifest_index,
        "title": track.title,
        "artist": track.artist,
        "relative_audio_path": track.relative_audio_path,
        "source_sha256": audio_sha256,
        "source_bytes": audio_bytes,
        "output_text": answer,
        "output_text_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "generated_token_count": int(sidecar["generated_token_count"]),
        "max_new_tokens": int(sidecar["generation_controls"]["max_new_tokens"]),
        "contract": CONTRACT,
        "attempt_id": f"{campaign_id}-local-{manifest_index:04d}",
        "canonical_source": CANONICAL_SOURCE,
    }
    if track.source_url:
        row["source_url"] = track.source_url
    row["provenance"] = {
        "runtime": "local-moss-music/mlx-8bit",
        "model_path": sidecar["model_path"],
        "prompt_sha256": sidecar["prompt_sha256"],
        "generation_controls": sidecar["generation_controls"],
        "generation_rate_tok_s": sidecar["generation_rate_tok_s"],
        "elapsed_seconds": sidecar["elapsed_seconds"],
        "think_present": sidecar["think_present"],
        "sidecar_path": sidecar.get("sidecar_path") or sidecar.get("answer_path"),
        "created_utc": sidecar["created_utc"],
    }
    return row


def write_delivery_jsonl(rows: list[dict[str, Any]], path: str | Path) -> Path:
    """Write strict LF JSONL: UTF-8, no BOM, LF separators, single trailing LF."""

    if not rows:
        raise ValueError("refusing to write an empty delivery")
    payload = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
    )
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload.encode("utf-8"))
    return out

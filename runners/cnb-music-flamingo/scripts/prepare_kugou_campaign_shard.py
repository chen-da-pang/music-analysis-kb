#!/usr/bin/env python3
"""Prepare one deterministic, sparse-Git-LFS Music Flamingo campaign shard.

The full KuGou manifest remains a small ordinary Git file.  This helper selects
one static range, removes only already durable successes, and writes a tiny
manifest plus Git-LFS include list for the current CNB run.  It deliberately
does not fetch or inspect unselected audio objects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Sequence

from music_flamingo_campaign import (
    CampaignError,
    CampaignItem,
    RuntimeContract,
    build_runtime_contract,
    load_campaign_manifest_items,
    pending_campaign_items,
)


class CampaignShardError(ValueError):
    """Raised for invalid static campaign shard planning inputs."""


def shard_bounds(*, total_items: int, shard_index: int, shard_count: int) -> tuple[int, int]:
    """Return a zero-based half-open, balanced static range for one shard."""
    if isinstance(total_items, bool) or not isinstance(total_items, int) or total_items < 1:
        raise CampaignShardError("total_items must be a positive integer")
    if isinstance(shard_count, bool) or not isinstance(shard_count, int) or not 1 <= shard_count <= total_items:
        raise CampaignShardError("shard_count must be between 1 and total_items")
    if isinstance(shard_index, bool) or not isinstance(shard_index, int) or not 1 <= shard_index <= shard_count:
        raise CampaignShardError("shard_index must be between 1 and shard_count")
    base, remainder = divmod(total_items, shard_count)
    start = (shard_index - 1) * base + min(shard_index - 1, remainder)
    end = start + base + (1 if shard_index <= remainder else 0)
    return start, end


def _manifest_record(item: CampaignItem) -> dict[str, object]:
    return {
        "id": item.item_id,
        "relative_audio_path": item.relative_audio_path,
        "source_bytes": item.source_bytes,
        "sha256": item.sha256,
        "title": item.title,
        "artist": item.artist,
        "campaign_id": item.campaign_id,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def _write_jsonl(path: Path, items: Sequence[CampaignItem]) -> None:
    _atomic_write_text(
        path,
        "".join(json.dumps(_manifest_record(item), ensure_ascii=False, separators=(",", ":")) + "\n" for item in items),
    )


def _repo_relative_lfs_path(repo_root: Path, input_root: Path, item: CampaignItem) -> str:
    try:
        prefix = input_root.resolve().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise CampaignShardError(f"input_root {input_root} is not inside repo_root {repo_root}") from exc
    return (prefix / Path(item.relative_audio_path)).as_posix()


def _manifest_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_campaign_shard(
    *,
    source_manifest: Path,
    input_root: Path,
    repo_root: Path,
    ledger_path: Path,
    run_dir: Path,
    expected_count: int,
    campaign_id: str,
    shard_index: int,
    shard_count: int,
    contract: RuntimeContract,
    max_pending_items: int = 0,
) -> dict[str, object]:
    """Write the current shard manifest, LFS include list, and global pending view."""
    if isinstance(max_pending_items, bool) or not isinstance(max_pending_items, int) or max_pending_items < 0:
        raise CampaignShardError("max_pending_items must be a non-negative integer")
    source_manifest = Path(source_manifest)
    input_root = Path(input_root)
    repo_root = Path(repo_root)
    run_dir = Path(run_dir)
    items = load_campaign_manifest_items(
        source_manifest,
        input_root,
        expected_count=expected_count,
        expected_campaign_id=campaign_id,
    )
    start, end = shard_bounds(total_items=len(items), shard_index=shard_index, shard_count=shard_count)
    shard_items = items[start:end]
    static_pending_shard_items = pending_campaign_items(shard_items, ledger_path, contract.fingerprint)
    # A bounded selection is only used by the no-inference CNB preflight.  It
    # must retain source order and never alter the static four-shard mapping.
    pending_shard_items = (
        static_pending_shard_items[:max_pending_items] if max_pending_items else static_pending_shard_items
    )
    global_pending_items = pending_campaign_items(items, ledger_path, contract.fingerprint)

    run_dir.mkdir(parents=True, exist_ok=True)
    shard_manifest = run_dir / "campaign_shard_manifest.jsonl"
    global_pending_manifest = run_dir / "campaign_global_pending_manifest.jsonl"
    include_path = run_dir / "lfs_include.txt"
    plan_path = run_dir / "campaign_shard_plan.json"
    _write_jsonl(shard_manifest, pending_shard_items)
    _write_jsonl(global_pending_manifest, global_pending_items)
    _atomic_write_text(
        include_path,
        "".join(_repo_relative_lfs_path(repo_root, input_root, item) + "\n" for item in pending_shard_items),
    )
    plan: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "source_manifest": str(source_manifest),
        "source_manifest_sha256": _manifest_sha256(source_manifest),
        "source_manifest_item_count": len(items),
        "contract": contract.fingerprint,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "source_range": {"start_index": start + 1, "end_index": end},
        "shard_item_count": len(shard_items),
        "static_shard_pending_item_count": len(static_pending_shard_items),
        "static_shard_pending_item_ids": [item.item_id for item in static_pending_shard_items],
        "max_pending_items": max_pending_items,
        "pending_item_count": len(pending_shard_items),
        "pending_item_ids": [item.item_id for item in pending_shard_items],
        "global_pending_item_count": len(global_pending_items),
        "global_pending_item_ids": [item.item_id for item in global_pending_items],
        "shard_manifest": str(shard_manifest),
        "global_pending_manifest": str(global_pending_manifest),
        "lfs_include_path": str(include_path),
    }
    _atomic_write_text(plan_path, json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return plan


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--runtime-image", required=True)
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file", type=Path)
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--audio-clip-seconds", type=float, default=240.0)
    parser.add_argument("--model-id", default="nvidia/music-flamingo-think-2601-hf")
    parser.add_argument("--model-revision", default="1ea2109")
    parser.add_argument("--model-dir", default="/opt/models/music-flamingo-think-2601-hf")
    parser.add_argument("--execution-profile", required=True)
    parser.add_argument("--runner-code-sha256")
    parser.add_argument(
        "--max-pending-items",
        type=int,
        default=0,
        help="Cap the selected pending items for a no-inference preflight; 0 keeps the full static shard.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.prompt is not None:
            prompt = args.prompt
        else:
            prompt = args.prompt_file.read_text(encoding="utf-8")
        contract = build_runtime_contract(
            args.runtime_image,
            prompt,
            args.max_new_tokens,
            args.audio_clip_seconds,
            runner_code_sha256=args.runner_code_sha256,
            model_id=args.model_id,
            model_revision=args.model_revision,
            model_dir=args.model_dir,
            execution_profile=args.execution_profile,
        )
        result = prepare_campaign_shard(
            source_manifest=args.source_manifest,
            input_root=args.input_root,
            repo_root=args.repo_root,
            ledger_path=args.ledger,
            run_dir=args.run_dir,
            expected_count=args.expected_count,
            campaign_id=args.campaign_id,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            contract=contract,
            max_pending_items=args.max_pending_items,
        )
    except (CampaignError, CampaignShardError, OSError) as exc:
        print(f"prepare_kugou_campaign_shard: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

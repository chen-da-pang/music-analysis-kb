#!/usr/bin/env python3
"""Prepare a selected KuGou subset for an isolated sparse-LFS quality rerun."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Sequence

from music_flamingo_campaign import CampaignError, CampaignItem, load_campaign_manifest_items


class QualityRerunError(ValueError):
    """Raised when the explicit quality-rerun selection is unsafe."""


def read_selection_indices(path: Path, *, source_count: int) -> list[int]:
    """Read strictly increasing one-based source-manifest indexes."""
    if source_count < 1:
        raise QualityRerunError("source_count must be positive")
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise QualityRerunError(f"Unable to read selection file {path}: {exc}") from exc
    indices: list[int] = []
    for line_number, raw in enumerate(lines, 1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        try:
            index = int(text)
        except ValueError as exc:
            raise QualityRerunError(f"Invalid manifest index at selection line {line_number}: {text!r}") from exc
        if not 1 <= index <= source_count:
            raise QualityRerunError(
                f"Selection index at line {line_number} is outside 1..{source_count}: {index}"
            )
        if indices and index <= indices[-1]:
            raise QualityRerunError("Selection indexes must be strictly increasing and unique")
        indices.append(index)
    if not indices:
        raise QualityRerunError("Selection file must contain at least one manifest index")
    return indices


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


def _manifest_row(item: CampaignItem) -> dict[str, object]:
    return {
        "id": item.item_id,
        "relative_audio_path": item.relative_audio_path,
        "source_bytes": item.source_bytes,
        "sha256": item.sha256,
        "title": item.title,
        "artist": item.artist,
        "campaign_id": item.campaign_id,
    }


def _manifest_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_quality_rerun(
    *,
    source_manifest: Path,
    input_root: Path,
    repo_root: Path,
    selection_file: Path,
    run_dir: Path,
    source_expected_count: int,
    expected_campaign_id: str,
) -> dict[str, object]:
    """Write a compact manifest and LFS include list for the selected rows."""
    source_manifest = Path(source_manifest)
    input_root = Path(input_root)
    repo_root = Path(repo_root)
    run_dir = Path(run_dir)
    items = load_campaign_manifest_items(
        source_manifest,
        input_root,
        expected_count=source_expected_count,
        expected_campaign_id=expected_campaign_id,
    )
    indices = read_selection_indices(selection_file, source_count=len(items))
    selected = [items[index - 1] for index in indices]
    try:
        input_prefix = input_root.resolve().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise QualityRerunError(f"input_root {input_root} is not inside repo_root {repo_root}") from exc

    rerun_manifest = run_dir / "quality_rerun_manifest.jsonl"
    include_path = run_dir / "quality_rerun_lfs_include.txt"
    plan_path = run_dir / "quality_rerun_plan.json"
    _atomic_write_text(
        rerun_manifest,
        "".join(json.dumps(_manifest_row(item), ensure_ascii=False, separators=(",", ":")) + "\n" for item in selected),
    )
    _atomic_write_text(
        include_path,
        "".join((input_prefix / item.relative_audio_path).as_posix() + "\n" for item in selected),
    )
    plan = {
        "schema_version": 1,
        "campaign_id": expected_campaign_id,
        "source_manifest": str(source_manifest),
        "source_manifest_sha256": _manifest_sha256(source_manifest),
        "source_manifest_item_count": len(items),
        "source_manifest_indices": indices,
        "selected_item_count": len(selected),
        "selected_item_ids": [item.item_id for item in selected],
        "rerun_manifest": str(rerun_manifest),
        "lfs_include_path": str(include_path),
    }
    _atomic_write_text(plan_path, json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return plan


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--selection-file", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-expected-count", type=int, required=True)
    parser.add_argument("--expected-campaign-id", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = prepare_quality_rerun(
            source_manifest=args.source_manifest,
            input_root=args.input_root,
            repo_root=args.repo_root,
            selection_file=args.selection_file,
            run_dir=args.run_dir,
            source_expected_count=args.source_expected_count,
            expected_campaign_id=args.expected_campaign_id,
        )
    except (CampaignError, QualityRerunError, OSError) as exc:
        print(f"prepare_kugou_quality_rerun: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

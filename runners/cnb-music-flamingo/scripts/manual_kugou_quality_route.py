#!/usr/bin/env python3
"""Validate the manual-only selected KuGou quality-rerun route.

This deliberately performs no LFS hydrate, ledger restore, model import, or
inference.  It proves that the caller has supplied a compact, explicit
selection and an isolated quality ledger *before* a Dev GPU workspace can do
any expensive work.  In particular, changing GPU execution profiles must not
turn a completed primary campaign into new pending work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Sequence

from music_flamingo_campaign import CampaignError, load_campaign_manifest_items
from prepare_kugou_quality_rerun import QualityRerunError, read_selection_indices


class ManualQualityRouteError(ValueError):
    """Raised when a manual selected-quality request is not isolated enough."""


_GPU_PROFILES = {
    "L40": "nvidia-l40/full_precision/bfloat16",
    "H20": "nvidia-h20/full_precision/bfloat16",
}
_MIN_FREE_MIB = {
    # The L40 full-precision load has previously occupied roughly 31 GiB.  A
    # 40 GiB pre-load floor leaves room for the model and rejects a polluted
    # shared allocation rather than discovering it via a CUDA OOM.
    "L40": 40_000,
    # H20 is a 96 GiB allocation.  The higher floor is intentionally the
    # already-proven clean-allocation threshold from the weekly recovery run.
    "H20": 87_000,
}
_SAFE_BRANCH_SUFFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_MANUAL_SELECTED_COUNT = 5
_REQUIRED_REPETITION_PENALTY = "1.08"
_REQUIRED_NO_REPEAT_NGRAM_SIZE = "4"


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManualQualityRouteError(f"{field} must be a positive integer")
    return value


def _resolve_repo_path(repo_root: Path, value: object, *, field: str, kind: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ManualQualityRouteError(f"{field} must not be empty")
    candidate = Path(text)
    resolved = (candidate if candidate.is_absolute() else repo_root / candidate).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ManualQualityRouteError(f"{field} must stay inside repo_root: {resolved}") from exc
    if kind == "file" and not resolved.is_file():
        raise ManualQualityRouteError(f"{field} is not a file: {resolved}")
    if kind == "directory" and not resolved.is_dir():
        raise ManualQualityRouteError(f"{field} is not a directory: {resolved}")
    return resolved


def _quality_ledger_branch(campaign_id: str, ledger_branch: object) -> tuple[str, str]:
    branch = str(ledger_branch or "").strip()
    primary = f"campaign-results/{campaign_id}"
    prefix = primary + "-quality-rerun-"
    if branch == primary:
        raise ManualQualityRouteError(
            f"Manual quality route refuses the primary ledger branch: {primary}"
        )
    if not branch.startswith(prefix):
        raise ManualQualityRouteError(
            "Manual quality ledger branch must start with "
            f"{prefix!r}; received {branch!r}"
        )
    suffix = branch[len(prefix) :]
    if not _SAFE_BRANCH_SUFFIX.fullmatch(suffix):
        raise ManualQualityRouteError(
            "Manual quality ledger branch suffix must be a safe non-empty identifier"
        )
    return primary, branch


def _gpu_contract(expected_gpu: object, execution_profile: object, minimum_free_mib: object) -> tuple[str, int]:
    gpu = str(expected_gpu or "").strip().upper()
    expected_profile = _GPU_PROFILES.get(gpu)
    if expected_profile is None:
        raise ManualQualityRouteError(
            "Manual quality route supports only explicit L40 or H20 allocations; "
            f"received {expected_gpu!r}"
        )
    if str(execution_profile or "").strip() != expected_profile:
        raise ManualQualityRouteError(
            f"MUSIC_FLAMINGO_EXECUTION_PROFILE must be {expected_profile!r} for {gpu}"
        )
    minimum = _positive_integer(minimum_free_mib, field="minimum_free_mib")
    required_minimum = _MIN_FREE_MIB[gpu]
    if minimum < required_minimum:
        raise ManualQualityRouteError(
            f"minimum_free_mib={minimum} is below the safe {gpu} floor of {required_minimum}"
        )
    return gpu, minimum


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_manifest_sha256(source_manifest: Path, expected_sha256: object) -> str:
    expected = str(expected_sha256 or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ManualQualityRouteError("source_manifest_sha256 must be 64 lowercase hexadecimal characters")
    actual = _sha256_file(source_manifest)
    if actual != expected:
        raise ManualQualityRouteError(
            f"Source manifest SHA-256 mismatch: {actual} != {expected}"
        )
    return actual


def _quality_generation_controls(
    repetition_penalty: object,
    no_repeat_ngram_size: object,
) -> dict[str, float | int]:
    """Require the manual route's audited anti-repetition settings exactly.

    Normal campaigns retain their independent runtime defaults.  This selected
    quality route is only admissible when it records the exact controls that
    ``audit_kugou_quality_rerun.py`` requires, rather than merely accepting any
    syntactically valid generation setting.
    """
    if str(repetition_penalty).strip() != _REQUIRED_REPETITION_PENALTY:
        raise ManualQualityRouteError(
            "MUSIC_FLAMINGO_REPETITION_PENALTY must be exactly "
            f"{_REQUIRED_REPETITION_PENALTY} for the manual quality route"
        )
    if str(no_repeat_ngram_size).strip() != _REQUIRED_NO_REPEAT_NGRAM_SIZE:
        raise ManualQualityRouteError(
            "MUSIC_FLAMINGO_NO_REPEAT_NGRAM_SIZE must be exactly "
            f"{_REQUIRED_NO_REPEAT_NGRAM_SIZE} for the manual quality route"
        )
    return {
        "repetition_penalty": float(_REQUIRED_REPETITION_PENALTY),
        "no_repeat_ngram_size": int(_REQUIRED_NO_REPEAT_NGRAM_SIZE),
    }


def validate_manual_quality_request(
    *,
    repo_root: Path,
    source_manifest: Path,
    input_root: Path,
    selection_file: Path,
    source_manifest_sha256: str,
    source_expected_count: int,
    expected_count: int,
    campaign_id: str,
    ledger_branch: str,
    expected_gpu: str,
    execution_profile: str,
    minimum_free_mib: int,
    max_selected_count: int,
    max_utilization_percent: int,
    repetition_penalty: object,
    no_repeat_ngram_size: object,
) -> dict[str, object]:
    """Validate an explicit selection without consulting any campaign ledger."""
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ManualQualityRouteError(f"repo_root is not a directory: {root}")
    source_manifest = _resolve_repo_path(root, source_manifest, field="source_manifest", kind="file")
    input_root = _resolve_repo_path(root, input_root, field="input_root", kind="directory")
    selection_file = _resolve_repo_path(root, selection_file, field="selection_file", kind="file")
    source_expected_count = _positive_integer(source_expected_count, field="source_expected_count")
    expected_count = _positive_integer(expected_count, field="expected_count")
    max_selected_count = _positive_integer(max_selected_count, field="max_selected_count")
    if max_selected_count > _MAX_MANUAL_SELECTED_COUNT:
        raise ManualQualityRouteError(
            "max_selected_count must not exceed the manual quality-route ceiling of "
            f"{_MAX_MANUAL_SELECTED_COUNT}"
        )
    if expected_count > max_selected_count:
        raise ManualQualityRouteError(
            f"expected_count={expected_count} exceeds manual maximum {max_selected_count}"
        )
    if max_utilization_percent != 0:
        raise ManualQualityRouteError("max_utilization_percent must be exactly 0 for a clean manual GPU gate")
    campaign_id = str(campaign_id or "").strip()
    if not campaign_id:
        raise ManualQualityRouteError("campaign_id must not be empty")
    primary_ledger_branch, isolated_ledger_branch = _quality_ledger_branch(campaign_id, ledger_branch)
    gpu, minimum_free_mib = _gpu_contract(expected_gpu, execution_profile, minimum_free_mib)
    generation_controls = _quality_generation_controls(repetition_penalty, no_repeat_ngram_size)
    actual_manifest_sha256 = _validate_manifest_sha256(source_manifest, source_manifest_sha256)
    items = load_campaign_manifest_items(
        source_manifest,
        input_root,
        expected_count=source_expected_count,
        expected_campaign_id=campaign_id,
    )
    indices = read_selection_indices(selection_file, source_count=len(items))
    if len(indices) != expected_count:
        raise ManualQualityRouteError(
            f"Selection contains {len(indices)} items, expected exactly {expected_count}"
        )
    if len(indices) > max_selected_count:
        raise ManualQualityRouteError(
            f"Selection contains {len(indices)} items, exceeds manual maximum {max_selected_count}"
        )
    selected = [items[index - 1] for index in indices]
    return {
        "schema_version": 1,
        "manual_only": True,
        "campaign_id": campaign_id,
        "source_manifest": str(source_manifest.relative_to(root)),
        "source_manifest_sha256": actual_manifest_sha256,
        "source_manifest_item_count": len(items),
        "selection_file": str(selection_file.relative_to(root)),
        "selected_source_indices": indices,
        "selected_item_ids": [item.item_id for item in selected],
        "selected_item_count": len(selected),
        "max_selected_count": max_selected_count,
        "primary_ledger_branch_rejected": primary_ledger_branch,
        "isolated_ledger_branch": isolated_ledger_branch,
        "expected_gpu": gpu,
        "execution_profile": execution_profile,
        "minimum_free_mib": minimum_free_mib,
        "max_utilization_percent": max_utilization_percent,
        "generation_controls": generation_controls,
    }


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
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
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--selection-file", type=Path, required=True)
    parser.add_argument("--source-manifest-sha256", required=True)
    parser.add_argument("--source-expected-count", type=int, required=True)
    parser.add_argument("--expected-count", type=int, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--ledger-branch", required=True)
    parser.add_argument("--expected-gpu", required=True)
    parser.add_argument("--execution-profile", required=True)
    parser.add_argument("--minimum-free-mib", type=int, required=True)
    parser.add_argument("--max-selected-count", type=int, default=5)
    parser.add_argument("--max-utilization-percent", type=int, default=0)
    parser.add_argument("--repetition-penalty", required=True)
    parser.add_argument("--no-repeat-ngram-size", required=True)
    parser.add_argument("--receipt", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = validate_manual_quality_request(
            repo_root=args.repo_root,
            source_manifest=args.source_manifest,
            input_root=args.input_root,
            selection_file=args.selection_file,
            source_manifest_sha256=args.source_manifest_sha256,
            source_expected_count=args.source_expected_count,
            expected_count=args.expected_count,
            campaign_id=args.campaign_id,
            ledger_branch=args.ledger_branch,
            expected_gpu=args.expected_gpu,
            execution_profile=args.execution_profile,
            minimum_free_mib=args.minimum_free_mib,
            max_selected_count=args.max_selected_count,
            max_utilization_percent=args.max_utilization_percent,
            repetition_penalty=args.repetition_penalty,
            no_repeat_ngram_size=args.no_repeat_ngram_size,
        )
        if args.receipt is not None:
            _atomic_write_json(args.receipt, result)
    except (CampaignError, ManualQualityRouteError, QualityRerunError, OSError) as exc:
        print(f"manual_kugou_quality_route: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

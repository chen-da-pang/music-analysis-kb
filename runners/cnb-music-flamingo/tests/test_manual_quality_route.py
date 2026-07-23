#!/usr/bin/env python3
"""Regression tests for the isolated Dev GPU manual quality route."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def source_row(index: int, *, campaign_id: str = "campaign-a") -> dict[str, object]:
    payload = f"audio-{index}".encode()
    return {
        "id": f"track-{index:03d}",
        "relative_audio_path": f"audio/track-{index:03d}.mp3",
        "source_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "title": f"Track {index}",
        "artist": f"Artist {index}",
        "campaign_id": campaign_id,
    }


class ManualQualityRouteTests(unittest.TestCase):
    def _request(
        self,
        root: Path,
        *,
        selection: str,
        ledger_branch: str,
        expected_gpu: str = "L40",
        execution_profile: str = "nvidia-l40/full_precision/bfloat16",
        minimum_free_mib: int = 40_000,
        source_count: int = 3,
    ) -> dict[str, object]:
        from manual_kugou_quality_route import validate_manual_quality_request

        input_root = root / "input"
        (input_root / "audio").mkdir(parents=True)
        manifest = input_root / "manifest.jsonl"
        manifest.write_text(
            "".join(json.dumps(source_row(index)) + "\n" for index in range(1, source_count + 1)),
            encoding="utf-8",
        )
        selection_path = root / "selection.txt"
        selection_path.write_text(selection, encoding="utf-8")
        manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
        return validate_manual_quality_request(
            repo_root=root,
            source_manifest=manifest,
            input_root=input_root,
            selection_file=selection_path,
            source_manifest_sha256=manifest_sha256,
            source_expected_count=source_count,
            expected_count=len([line for line in selection.splitlines() if line.strip()]),
            campaign_id="campaign-a",
            ledger_branch=ledger_branch,
            expected_gpu=expected_gpu,
            execution_profile=execution_profile,
            minimum_free_mib=minimum_free_mib,
            max_selected_count=5,
            max_utilization_percent=0,
        )

    def test_accepts_only_explicit_subset_and_isolated_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self._request(
                Path(temp_dir),
                selection="1\n3\n",
                ledger_branch="campaign-results/campaign-a-quality-rerun-l40-probe-1",
            )
        self.assertTrue(result["manual_only"])
        self.assertEqual(result["selected_source_indices"], [1, 3])
        self.assertEqual(result["selected_item_ids"], ["track-001", "track-003"])
        self.assertEqual(result["primary_ledger_branch_rejected"], "campaign-results/campaign-a")

    def test_rejects_primary_or_non_quality_ledger_branch(self) -> None:
        from manual_kugou_quality_route import ManualQualityRouteError

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ManualQualityRouteError, "primary ledger branch"):
                self._request(root, selection="1\n", ledger_branch="campaign-results/campaign-a")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ManualQualityRouteError, "must start"):
                self._request(root, selection="1\n", ledger_branch="campaign-results/campaign-a-retry")

    def test_profile_switch_cannot_replay_completed_229_item_primary_campaign(self) -> None:
        """A profile change has no path to the primary ledger or a full planner."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            (input_root / "audio").mkdir(parents=True)
            manifest = input_root / "manifest.jsonl"
            manifest.write_text(
                "".join(json.dumps(source_row(index)) + "\n" for index in range(1, 230)),
                encoding="utf-8",
            )
            selection = root / "selection.txt"
            selection.write_text("59\n", encoding="utf-8")

            from manual_kugou_quality_route import validate_manual_quality_request

            common = {
                "repo_root": root,
                "source_manifest": manifest,
                "input_root": input_root,
                "selection_file": selection,
                "source_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "source_expected_count": 229,
                "expected_count": 1,
                "campaign_id": "campaign-a",
                "max_selected_count": 5,
                "max_utilization_percent": 0,
            }
            l40 = validate_manual_quality_request(
                **common,
                ledger_branch="campaign-results/campaign-a-quality-rerun-l40-probe-1",
                expected_gpu="L40",
                execution_profile="nvidia-l40/full_precision/bfloat16",
                minimum_free_mib=40_000,
            )
            h20 = validate_manual_quality_request(
                **common,
                ledger_branch="campaign-results/campaign-a-quality-rerun-h20-probe-1",
                expected_gpu="H20",
                execution_profile="nvidia-h20/full_precision/bfloat16",
                minimum_free_mib=87_000,
            )

        self.assertEqual(l40["source_manifest_item_count"], 229)
        self.assertEqual(h20["source_manifest_item_count"], 229)
        self.assertEqual(l40["selected_item_count"], 1)
        self.assertEqual(h20["selected_item_count"], 1)
        self.assertEqual(l40["selected_item_ids"], ["track-059"])
        self.assertEqual(h20["selected_item_ids"], ["track-059"])
        self.assertEqual(l40["isolated_ledger_branch"], "campaign-results/campaign-a-quality-rerun-l40-probe-1")
        self.assertEqual(h20["isolated_ledger_branch"], "campaign-results/campaign-a-quality-rerun-h20-probe-1")

    def test_rejects_profile_mismatch_and_weakened_gpu_floor(self) -> None:
        from manual_kugou_quality_route import ManualQualityRouteError

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ManualQualityRouteError, "EXECUTION_PROFILE"):
                self._request(
                    root,
                    selection="1\n",
                    ledger_branch="campaign-results/campaign-a-quality-rerun-l40-probe-1",
                    expected_gpu="L40",
                    execution_profile="nvidia-h20/full_precision/bfloat16",
                )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ManualQualityRouteError, "safe L40 floor"):
                self._request(
                    root,
                    selection="1\n",
                    ledger_branch="campaign-results/campaign-a-quality-rerun-l40-probe-1",
                    minimum_free_mib=39_999,
                )

    def test_rejects_a_manifest_that_does_not_match_the_receipt_hash(self) -> None:
        from manual_kugou_quality_route import ManualQualityRouteError, validate_manual_quality_request

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            (input_root / "audio").mkdir(parents=True)
            manifest = input_root / "manifest.jsonl"
            manifest.write_text(json.dumps(source_row(1)) + "\n", encoding="utf-8")
            selection = root / "selection.txt"
            selection.write_text("1\n", encoding="utf-8")
            with self.assertRaisesRegex(ManualQualityRouteError, "SHA-256 mismatch"):
                validate_manual_quality_request(
                    repo_root=root,
                    source_manifest=manifest,
                    input_root=input_root,
                    selection_file=selection,
                    source_manifest_sha256="0" * 64,
                    source_expected_count=1,
                    expected_count=1,
                    campaign_id="campaign-a",
                    ledger_branch="campaign-results/campaign-a-quality-rerun-l40-probe-1",
                    expected_gpu="L40",
                    execution_profile="nvidia-l40/full_precision/bfloat16",
                    minimum_free_mib=40_000,
                    max_selected_count=5,
                    max_utilization_percent=0,
                )


if __name__ == "__main__":
    unittest.main()

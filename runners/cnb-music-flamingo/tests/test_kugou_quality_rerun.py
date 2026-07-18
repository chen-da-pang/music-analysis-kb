#!/usr/bin/env python3
"""Tests for the explicit sparse-LFS KuGou quality-rerun selector."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def source_row(index: int) -> dict[str, object]:
    payload = f"audio-{index}".encode()
    return {
        "id": f"track-{index}",
        "relative_audio_path": f"audio/track-{index}.mp3",
        "source_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "title": f"Track {index}",
        "artist": f"Artist {index}",
        "campaign_id": "campaign-a",
    }


class QualityRerunPreparationTests(unittest.TestCase):
    def test_selected_rows_keep_source_order_and_emit_sparse_include_list(self) -> None:
        from prepare_kugou_quality_rerun import prepare_quality_rerun

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            (input_root / "audio").mkdir(parents=True)
            source_manifest = input_root / "manifest.jsonl"
            rows = [source_row(index) for index in range(1, 5)]
            source_manifest.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            selection = root / "selection.txt"
            selection.write_text("2\n4\n", encoding="utf-8")
            run_dir = root / "run"

            plan = prepare_quality_rerun(
                source_manifest=source_manifest,
                input_root=input_root,
                repo_root=root,
                selection_file=selection,
                run_dir=run_dir,
                source_expected_count=4,
                expected_campaign_id="campaign-a",
            )

            self.assertEqual(plan["source_manifest_indices"], [2, 4])
            self.assertEqual(plan["selected_item_count"], 2)
            selected = [json.loads(line) for line in (run_dir / "quality_rerun_manifest.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["id"] for row in selected], ["track-2", "track-4"])
            self.assertEqual(
                (run_dir / "quality_rerun_lfs_include.txt").read_text(encoding="utf-8").splitlines(),
                ["input/audio/track-2.mp3", "input/audio/track-4.mp3"],
            )

    def test_selection_rejects_duplicates_and_out_of_range_indexes(self) -> None:
        from prepare_kugou_quality_rerun import QualityRerunError, read_selection_indices

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "selection.txt"
            path.write_text("2\n2\n", encoding="utf-8")
            with self.assertRaises(QualityRerunError):
                read_selection_indices(path, source_count=3)
            path.write_text("4\n", encoding="utf-8")
            with self.assertRaises(QualityRerunError):
                read_selection_indices(path, source_count=3)

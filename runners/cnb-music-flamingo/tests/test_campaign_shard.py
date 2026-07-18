#!/usr/bin/env python3
"""Tests for deterministic sparse-LFS KuGou campaign shard planning."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def row(index: int) -> dict[str, object]:
    item_id = f"song-{index:03d}"
    payload = f"audio-{index}".encode("utf-8")
    return {
        "id": item_id,
        "relative_audio_path": f"audio/{item_id}.flac",
        "source_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "title": f"Title {index}",
        "artist": f"Artist {index}",
        "campaign_id": "campaign-a",
    }


class CampaignShardTests(unittest.TestCase):
    def test_static_ranges_are_complete_disjoint_and_balanced(self) -> None:
        from prepare_kugou_campaign_shard import shard_bounds

        ranges = [shard_bounds(total_items=927, shard_index=index, shard_count=4) for index in range(1, 5)]
        self.assertEqual(ranges, [(0, 232), (232, 464), (464, 696), (696, 927)])
        flattened = [index for start, end in ranges for index in range(start, end)]
        self.assertEqual(flattened, list(range(927)))

    def test_prepare_shard_writes_only_pending_lfs_paths_and_preserves_source_index(self) -> None:
        from music_flamingo_campaign import append_campaign_ledger, build_runtime_contract, load_campaign_manifest_items, make_campaign_ledger_record
        from prepare_kugou_campaign_shard import prepare_campaign_shard

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo_root = root / "repo"
            input_root = repo_root / "data/input/campaign"
            (input_root / "audio").mkdir(parents=True)
            rows = [row(index) for index in range(1, 7)]
            manifest = input_root / "manifest.jsonl"
            manifest.write_text("".join(json.dumps(value) + "\n" for value in rows), encoding="utf-8")
            ledger = root / "campaign_ledger.jsonl"
            items = load_campaign_manifest_items(manifest, input_root, expected_count=6, expected_campaign_id="campaign-a")
            contract = build_runtime_contract(
                "registry.example/music@sha256:" + "a" * 64,
                "prompt",
                2048,
                240,
                runner_code_sha256="b" * 64,
                execution_profile="nvidia-l40/full_precision/bfloat16",
            )
            output_text = "already done\n"
            append_campaign_ledger(
                ledger,
                make_campaign_ledger_record(
                    items[0],
                    contract,
                    status="success",
                    attempt_id="attempt-a",
                    output_text=output_text,
                    output_text_sha256=hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
                ),
            )

            run_dir = root / "run"
            plan = prepare_campaign_shard(
                source_manifest=manifest,
                input_root=input_root,
                repo_root=repo_root,
                ledger_path=ledger,
                run_dir=run_dir,
                expected_count=6,
                campaign_id="campaign-a",
                shard_index=1,
                shard_count=2,
                contract=contract,
            )

            self.assertEqual(plan["source_range"], {"start_index": 1, "end_index": 3})
            self.assertEqual(plan["shard_item_count"], 3)
            self.assertEqual(plan["pending_item_count"], 2)
            selected = [json.loads(line) for line in (run_dir / "campaign_shard_manifest.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([entry["id"] for entry in selected], ["song-002", "song-003"])
            self.assertEqual([entry["relative_audio_path"] for entry in selected], ["audio/song-002.flac", "audio/song-003.flac"])
            include_lines = (run_dir / "lfs_include.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                include_lines,
                [
                    "data/input/campaign/audio/song-002.flac",
                    "data/input/campaign/audio/song-003.flac",
                ],
            )
            self.assertEqual(plan["global_pending_item_count"], 5)

    def test_preflight_cap_fetches_one_pending_item_without_changing_static_shard_membership(self) -> None:
        from music_flamingo_campaign import build_runtime_contract
        from prepare_kugou_campaign_shard import prepare_campaign_shard

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo_root = root / "repo"
            input_root = repo_root / "data/input/campaign"
            (input_root / "audio").mkdir(parents=True)
            rows = [row(index) for index in range(1, 7)]
            manifest = input_root / "manifest.jsonl"
            manifest.write_text("".join(json.dumps(value) + "\n" for value in rows), encoding="utf-8")
            contract = build_runtime_contract(
                "registry.example/music@sha256:" + "a" * 64,
                "prompt",
                2048,
                240,
                runner_code_sha256="b" * 64,
                execution_profile="nvidia-l40/full_precision/bfloat16",
            )

            plan = prepare_campaign_shard(
                source_manifest=manifest,
                input_root=input_root,
                repo_root=repo_root,
                ledger_path=root / "campaign_ledger.jsonl",
                run_dir=root / "preflight",
                expected_count=6,
                campaign_id="campaign-a",
                shard_index=1,
                shard_count=2,
                contract=contract,
                max_pending_items=1,
            )

            self.assertEqual(plan["source_range"], {"start_index": 1, "end_index": 3})
            self.assertEqual(plan["static_shard_pending_item_count"], 3)
            self.assertEqual(plan["pending_item_count"], 1)
            self.assertEqual(plan["pending_item_ids"], ["song-001"])
            self.assertEqual(
                (root / "preflight/lfs_include.txt").read_text(encoding="utf-8").splitlines(),
                ["data/input/campaign/audio/song-001.flac"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

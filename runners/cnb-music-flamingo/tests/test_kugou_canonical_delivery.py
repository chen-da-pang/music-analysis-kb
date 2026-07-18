#!/usr/bin/env python3
"""Tests for direct and quality-rerun canonical campaign delivery."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def record(item: dict[str, object], output: str, *, attempt: str, contract: str, tokens: int, controls: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "success",
        "id": item["id"],
        "manifest_index": item["manifest_index"],
        "attempt_id": attempt,
        "relative_audio_path": item["relative_audio_path"],
        "source_sha256": item["sha256"],
        "source_bytes": item["source_bytes"],
        "contract": contract,
        "max_new_tokens": 1400 if controls else 2048,
        "generated_token_count": tokens,
        "output_text": output,
        "output_text_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }
    if controls:
        payload["generation_controls"] = {"repetition_penalty": 1.08, "no_repeat_ngram_size": 4}
    return payload


class CanonicalDeliveryTests(unittest.TestCase):
    def write_fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        items = [
            {
                "id": "song-a",
                "relative_audio_path": "audio/a.flac",
                "source_bytes": 11,
                "sha256": "a" * 64,
                "title": "A",
                "artist": "Artist A",
                "campaign_id": "campaign-test",
                "manifest_index": 1,
                "source_url": "https://www.kugou.com/mixsong/agent_gateway/song-a.html",
            },
            {
                "id": "song-b",
                "relative_audio_path": "audio/b.flac",
                "source_bytes": 12,
                "sha256": "b" * 64,
                "title": "B",
                "artist": "Artist B",
                "campaign_id": "campaign-test",
                "manifest_index": 2,
            },
            {
                "id": "song-c",
                "relative_audio_path": "audio/c.flac",
                "source_bytes": 13,
                "sha256": "c" * 64,
                "title": "C",
                "artist": "Artist C",
                "campaign_id": "campaign-test",
                "manifest_index": 3,
            },
        ]
        source_manifest = root / "source.jsonl"
        source_manifest.write_text(
            "".join(json.dumps({k: v for k, v in item.items() if k != "manifest_index"}) + "\n" for item in items),
            encoding="utf-8",
        )
        selection = root / "selection.txt"
        selection.write_text("2\n", encoding="utf-8")
        campaign = root / "campaign.jsonl"
        stale = record(items[0], "stale historical analysis." * 30, attempt="stale", contract="base-contract", tokens=900, controls=False)
        stale["manifest_index"] = 99
        campaign.write_text(
            json.dumps(stale)
            + "\n"
            + "".join(
                json.dumps(record(item, f"base analysis for {item['id']}." * 30, attempt="base", contract="base-contract", tokens=900, controls=False))
                + "\n"
                for item in items
            ),
            encoding="utf-8",
        )
        rerun = root / "rerun.jsonl"
        rerun_output = ("quality rerun analysis for song-b with audible arrangement detail. " * 12).strip() + ".\n"
        rerun_record = record(items[1], rerun_output, attempt="rerun-attempt", contract="rerun-contract", tokens=777, controls=True)
        # The isolated rerun manifest is compact (1..N), unlike the full
        # source manifest where song-b is index 2.
        rerun_record["manifest_index"] = 1
        rerun.write_text(json.dumps(rerun_record) + "\n", encoding="utf-8")
        return source_manifest, selection, campaign, rerun

    def test_build_promotes_only_audited_selected_rows(self) -> None:
        from build_kugou_canonical_delivery import build_canonical_delivery

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, selection, campaign, rerun = self.write_fixture(root)
            output = root / "delivery.jsonl"
            state = root / "delivery-state.json"
            report = build_canonical_delivery(
                source_manifest=source,
                campaign_ledger=campaign,
                quality_ledger=rerun,
                selection_file=selection,
                quality_attempt_id="rerun-attempt",
                output_manifest=output,
                output_state=state,
                expected_count=3,
                expected_campaign_id="campaign-test",
            )
            self.assertEqual(report["status"], "pass")
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["id"] for row in rows], ["song-a", "song-b", "song-c"])
            self.assertEqual(rows[0]["canonical_source"], "campaign")
            self.assertEqual(
                rows[0]["source_url"],
                "https://www.kugou.com/mixsong/agent_gateway/song-a.html",
            )
            self.assertEqual(rows[1]["canonical_source"], "quality_rerun")
            self.assertEqual(rows[1]["attempt_id"], "rerun-attempt")
            self.assertEqual(rows[1]["superseded_campaign_attempt_id"], "base")
            self.assertEqual(rows[2]["canonical_source"], "campaign")
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["quality_rerun_source_count"], 1)

    def test_build_direct_campaign_delivery_requires_and_preserves_source_urls(self) -> None:
        from build_kugou_canonical_delivery import build_canonical_delivery

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _, campaign, _ = self.write_fixture(root)
            rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
            for index, row in enumerate(rows, 1):
                row["source_url"] = f"https://www.kugou.com/mixsong/agent_gateway/song-{index}.html"
            source.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            campaign_rows = [json.loads(line) for line in campaign.read_text(encoding="utf-8").splitlines()]
            for index, row in enumerate(campaign_rows, 1):
                output = (
                    f"A complete arrangement description for song {index} moves through an opening texture, "
                    "a contrasting middle section, and a resolved final cadence with changing instrumentation "
                    "and dynamics that remain easy to distinguish in the recording."
                )
                row["output_text"] = output
                row["output_text_sha256"] = hashlib.sha256(output.encode("utf-8")).hexdigest()
            campaign.write_text("".join(json.dumps(row) + "\n" for row in campaign_rows), encoding="utf-8")
            output = root / "delivery.jsonl"
            state = root / "delivery-state.json"
            report = build_canonical_delivery(
                source_manifest=source,
                campaign_ledger=campaign,
                output_manifest=output,
                output_state=state,
                expected_count=3,
                expected_campaign_id="campaign-test",
                require_source_url=True,
            )

            self.assertEqual(report["campaign_source_count"], 3)
            self.assertEqual(report["quality_rerun_source_count"], 0)
            self.assertEqual(report["source_url_count"], 3)
            delivery = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(row["canonical_source"] == "campaign" for row in delivery))
            self.assertEqual(len({row["source_url"] for row in delivery}), 3)

    def test_build_accepts_shard_local_campaign_indexes_and_rewrites_global_delivery_indexes(self) -> None:
        from build_kugou_canonical_delivery import build_canonical_delivery

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _, campaign, _ = self.write_fixture(root)
            rows = [json.loads(line) for line in campaign.read_text(encoding="utf-8").splitlines()]
            # Simulate three independent shard manifests: every ledger record
            # has a local coordinate that differs from the full source order.
            for local_index, row in enumerate(rows, 1):
                row["manifest_index"] = local_index + 100
                output = (
                    f"A shard-local analysis for song {local_index} describes the opening texture, "
                    "contrasting middle section, instrumental roles, dynamic movement, and a resolved "
                    "ending with enough distinct detail to pass the direct quality gate."
                )
                row["output_text"] = output
                row["output_text_sha256"] = hashlib.sha256(output.encode("utf-8")).hexdigest()
            campaign.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            output = root / "delivery.jsonl"
            state = root / "delivery-state.json"
            report = build_canonical_delivery(
                source_manifest=source,
                campaign_ledger=campaign,
                output_manifest=output,
                output_state=state,
                expected_count=3,
                expected_campaign_id="campaign-test",
            )

            self.assertEqual(report["status"], "pass")
            delivery = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["manifest_index"] for row in delivery], [1, 2, 3])

    def test_build_ignores_historical_quality_error_before_success(self) -> None:
        from build_kugou_canonical_delivery import build_canonical_delivery

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, selection, campaign, rerun = self.write_fixture(root)
            success = json.loads(rerun.read_text(encoding="utf-8").strip())
            error = {
                "schema_version": 1,
                "status": "error",
                "id": success["id"],
                "attempt_id": success["attempt_id"],
                "relative_audio_path": success["relative_audio_path"],
                "source_sha256": success["source_sha256"],
                "source_bytes": success["source_bytes"],
                "error": "transient model load failure",
            }
            rerun.write_text(
                json.dumps(error) + "\n" + json.dumps(success) + "\n",
                encoding="utf-8",
            )

            report = build_canonical_delivery(
                source_manifest=source,
                campaign_ledger=campaign,
                quality_ledger=rerun,
                selection_file=selection,
                quality_attempt_id="rerun-attempt",
                output_manifest=root / "delivery.jsonl",
                output_state=root / "delivery-state.json",
                expected_count=3,
                expected_campaign_id="campaign-test",
            )

            self.assertEqual(report["status"], "pass")

    def test_build_direct_campaign_delivery_rejects_token_cap(self) -> None:
        from build_kugou_canonical_delivery import CanonicalDeliveryError, build_canonical_delivery

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _, campaign, _ = self.write_fixture(root)
            records = [json.loads(line) for line in campaign.read_text(encoding="utf-8").splitlines()]
            records[1]["generated_token_count"] = records[1]["max_new_tokens"]
            campaign.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")

            with self.assertRaisesRegex(CanonicalDeliveryError, "reached token cap"):
                build_canonical_delivery(
                    source_manifest=source,
                    campaign_ledger=campaign,
                    output_manifest=root / "delivery.jsonl",
                    output_state=root / "delivery-state.json",
                    expected_count=3,
                    expected_campaign_id="campaign-test",
                )

    def test_build_rejects_token_cap_rerun_before_promoting_it(self) -> None:
        from build_kugou_canonical_delivery import CanonicalDeliveryError, build_canonical_delivery

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, selection, campaign, rerun = self.write_fixture(root)
            row = json.loads(rerun.read_text(encoding="utf-8"))
            row["generated_token_count"] = 1400
            rerun.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(CanonicalDeliveryError, "Quality rerun audit did not pass"):
                build_canonical_delivery(
                    source_manifest=source,
                    campaign_ledger=campaign,
                    quality_ledger=rerun,
                    selection_file=selection,
                    quality_attempt_id="rerun-attempt",
                    output_manifest=root / "delivery.jsonl",
                    output_state=root / "delivery-state.json",
                    expected_count=3,
                    expected_campaign_id="campaign-test",
                )

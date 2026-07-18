#!/usr/bin/env python3
"""Tests for the resumable, manifest-driven Music Flamingo campaign helpers."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def manifest_row(item_id: str, relative_audio_path: str, data: bytes, *, campaign_id: str = "campaign-a") -> dict:
    return {
        "id": item_id,
        "relative_audio_path": relative_audio_path,
        "source_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "title": f"Title {item_id}",
        "artist": f"Artist {item_id}",
        "campaign_id": campaign_id,
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def reusable_success_fields(output_text: str, manifest_index: int) -> dict:
    return {
        "output_text": output_text,
        "output_text_sha256": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
        "manifest_index": manifest_index,
        "attempt_id": "attempt-a",
        "runtime_image": "registry.example/music@sha256:" + "f" * 64,
        "prompt_sha256": "e" * 64,
        "max_new_tokens": 2048,
        "audio_clip_seconds": 240.0,
        "model_id": "nvidia/music-flamingo-think-2601-hf",
        "model_revision": "1ea2109",
        "model_dir": "/opt/models/music-flamingo-think-2601-hf",
        "execution_profile": "nvidia-l40/full_precision/bfloat16",
        "runner_code_sha256": "d" * 64,
    }


class CampaignManifestTests(unittest.TestCase):
    def test_load_campaign_items_validates_files_and_preserves_manifest_order(self) -> None:
        from music_flamingo_campaign import CampaignManifestError, load_campaign_items

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_root = root / "input"
            (audio_root / "audio").mkdir(parents=True)
            first_data = b"first-audio"
            second_data = b"second-audio"
            (audio_root / "audio/second.flac").write_bytes(second_data)
            (audio_root / "audio/first.flac").write_bytes(first_data)
            manifest = root / "manifest.jsonl"
            write_jsonl(
                manifest,
                [
                    manifest_row("first", "audio/first.flac", first_data),
                    manifest_row("second", "audio/second.flac", second_data),
                ],
            )

            items = load_campaign_items(manifest, audio_root, expected_count=2)

            self.assertEqual([item.item_id for item in items], ["first", "second"])
            self.assertEqual(items[0].id, "first")
            self.assertEqual(items[0].audio_path, (audio_root / "audio/first.flac").resolve())
            self.assertEqual(items[1].campaign_id, "campaign-a")

            with self.assertRaises(CampaignManifestError):
                load_campaign_items(
                    manifest,
                    audio_root,
                    expected_count=2,
                    expected_campaign_id="campaign-b",
                )

    def test_load_campaign_items_rejects_unsafe_or_changed_inputs(self) -> None:
        from music_flamingo_campaign import CampaignManifestError, load_campaign_items

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_root = root / "input"
            (audio_root / "audio").mkdir(parents=True)
            data = b"valid-audio"
            (audio_root / "audio/song.flac").write_bytes(data)
            manifest = root / "manifest.jsonl"

            invalid_cases = {
                "path_escape": [manifest_row("song", "../outside.flac", data)],
                "duplicate_id": [
                    manifest_row("song", "audio/song.flac", data),
                    manifest_row("song", "audio/song.flac", data),
                ],
                "wrong_size": [{**manifest_row("song", "audio/song.flac", data), "source_bytes": len(data) + 1}],
                "wrong_digest": [{**manifest_row("song", "audio/song.flac", data), "sha256": "0" * 64}],
            }
            for name, rows in invalid_cases.items():
                with self.subTest(name=name):
                    write_jsonl(manifest, rows)
                    with self.assertRaises(CampaignManifestError):
                        load_campaign_items(manifest, audio_root, expected_count=len(rows))


class CampaignContractTests(unittest.TestCase):
    def test_contract_fingerprint_changes_for_each_execution_input(self) -> None:
        from music_flamingo_campaign import CampaignError, contract_fingerprint

        image_a = "registry.example/music@sha256:" + "a" * 64
        image_b = "registry.example/music@sha256:" + "b" * 64
        execution = {
            "execution_profile": "nvidia-l40/full_precision/bfloat16",
            "model_id": "nvidia/music-flamingo-think-2601-hf",
            "model_revision": "1ea2109",
            "model_dir": "/opt/models/music-flamingo-think-2601-hf",
        }

        base = contract_fingerprint(
            image_a,
            "Describe the music.",
            2048,
            240,
            **execution,
        )
        self.assertEqual(
            base,
            contract_fingerprint(
                image_a,
                "Describe the music.",
                2048,
                240.0,
                **execution,
            ),
        )
        for changed in (
            (image_b, "Describe the music.", 2048, 240),
            (image_a, "Use a different prompt.", 2048, 240),
            (image_a, "Describe the music.", 1024, 240),
            (image_a, "Describe the music.", 2048, 120),
        ):
            with self.subTest(changed=changed):
                self.assertNotEqual(base, contract_fingerprint(*changed, **execution))
        with self.assertRaises(CampaignError):
            contract_fingerprint("registry.example/music:mutable-tag", "Describe the music.", 2048, 240, **execution)
        self.assertNotEqual(
            contract_fingerprint(image_a, "Describe the music.", 2048, 240, runner_code_sha256="c" * 64, **execution),
            contract_fingerprint(image_a, "Describe the music.", 2048, 240, runner_code_sha256="d" * 64, **execution),
        )
        self.assertNotEqual(
            base,
            contract_fingerprint(image_a, "Describe the music.", 2048, 240, model_revision="other", **{
                key: value for key, value in execution.items() if key != "model_revision"
            }),
        )
        self.assertNotEqual(
            base,
            contract_fingerprint(image_a, "Describe the music.", 2048, 240, execution_profile="nvidia-l40/4bit/bfloat16", **{
                key: value for key, value in execution.items() if key != "execution_profile"
            }),
        )
        with self.assertRaises(CampaignError):
            contract_fingerprint(image_a, "Describe the music.", 2048, 240, execution_profile="", **{
                key: value for key, value in execution.items() if key != "execution_profile"
            })


class CampaignLedgerTests(unittest.TestCase):
    def test_success_record_binds_output_contract_and_execution_profile(self) -> None:
        from music_flamingo_campaign import (
            CampaignError,
            CampaignItem,
            CampaignLedgerError,
            build_runtime_contract,
            make_campaign_ledger_record,
            validate_execution_profile,
        )

        item = CampaignItem(
            "song-a",
            "audio/song-a.flac",
            Path("/tmp/song-a.flac"),
            12,
            "a" * 64,
            "",
            "",
            "campaign-a",
            manifest_index=1,
        )
        contract = build_runtime_contract(
            "registry.example/music@sha256:" + "b" * 64,
            "Describe the music.",
            2048,
            240,
            runner_code_sha256="c" * 64,
            model_id="nvidia/music-flamingo-think-2601-hf",
            model_revision="1ea2109",
            model_dir="/opt/models/music-flamingo-think-2601-hf",
            execution_profile="nvidia-l40/full_precision/bfloat16",
        )
        output_text = "A detailed music analysis.\n"
        record = make_campaign_ledger_record(
            item,
            contract,
            status="success",
            attempt_id="attempt-a",
            output_text=output_text,
            output_text_sha256=hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
        )

        self.assertEqual(record["id"], "song-a")
        self.assertEqual(record["manifest_index"], 1)
        self.assertEqual(record["runner_code_sha256"], "c" * 64)
        self.assertEqual(record["execution_profile"], "nvidia-l40/full_precision/bfloat16")
        validate_execution_profile(contract, "nvidia-l40/full_precision/bfloat16")
        with self.assertRaises(CampaignError):
            validate_execution_profile(contract, "nvidia-l40/4bit/bfloat16")
        with self.assertRaises(CampaignLedgerError):
            make_campaign_ledger_record(
                item,
                contract,
                status="success",
                attempt_id="attempt-a",
                output_text=output_text,
                output_text_sha256="0" * 64,
            )

    def test_append_campaign_ledger_is_newline_delimited_and_fsynced(self) -> None:
        from music_flamingo_campaign import append_campaign_ledger

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "nested/campaign_ledger.jsonl"
            record = {"status": "success", "id": "song-a", "contract": "same"}
            with mock.patch("music_flamingo_campaign.os.fsync") as fsync:
                append_campaign_ledger(ledger, record)

            self.assertEqual(json.loads(ledger.read_text(encoding="utf-8")), record)
            self.assertTrue(ledger.read_bytes().endswith(b"\n"))
            self.assertEqual(fsync.call_count, 2, "new ledger must sync both file and parent directory")

    def test_ledger_keeps_unicode_line_separators_inside_one_record(self) -> None:
        from music_flamingo_campaign import append_campaign_ledger, read_campaign_ledger

        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "campaign_ledger.jsonl"
            record = {
                "status": "success",
                "id": "song-a",
                "contract": "same",
                "output_text": "first\u2028second\u2029third\u0085fourth",
            }
            append_campaign_ledger(ledger, record)

            # The Unicode separators are content, not JSONL record boundaries.
            self.assertEqual(read_campaign_ledger(ledger), [record])

    def test_only_matching_contract_and_digest_successes_are_reusable(self) -> None:
        from music_flamingo_campaign import (
            CampaignItem,
            append_campaign_ledger,
            pending_campaign_items,
            read_successful_item_ids,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "campaign_ledger.jsonl"
            items = [
                CampaignItem("a", "audio/a.flac", root / "a.flac", 1, "a" * 64, "", "", "campaign-a", manifest_index=1),
                CampaignItem("b", "audio/b.flac", root / "b.flac", 1, "b" * 64, "", "", "campaign-a", manifest_index=2),
                CampaignItem("c", "audio/c.flac", root / "c.flac", 1, "c" * 64, "", "", "campaign-a", manifest_index=3),
                CampaignItem("d", "audio/d.flac", root / "d.flac", 1, "d" * 64, "", "", "campaign-a", manifest_index=4),
            ]
            append_campaign_ledger(ledger, {
                "status": "success", "id": "a", "source_sha256": "a" * 64, "source_bytes": 1,
                "contract": "same", **reusable_success_fields("output-a", 1),
            })
            append_campaign_ledger(ledger, {"status": "error", "id": "b", "source_sha256": "b" * 64, "source_bytes": 1, "contract": "same"})
            append_campaign_ledger(ledger, {
                "status": "success", "id": "c", "source_sha256": "c" * 64, "source_bytes": 1,
                "contract": "different", **reusable_success_fields("output-c", 3),
            })
            append_campaign_ledger(ledger, {
                "status": "success", "id": "d", "source_sha256": "e" * 64, "source_bytes": 1,
                "contract": "same", **reusable_success_fields("output-d", 4),
            })

            self.assertEqual(read_successful_item_ids(ledger, "same"), {"a", "d"})
            self.assertEqual(
                [item.item_id for item in pending_campaign_items(items, ledger, "same")],
                ["b", "c", "d"],
            )

    def test_pending_rejects_an_incomplete_matching_success_record(self) -> None:
        from music_flamingo_campaign import (
            CampaignItem,
            CampaignLedgerError,
            append_campaign_ledger,
            pending_campaign_items,
            read_successful_item_ids,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "campaign_ledger.jsonl"
            item = CampaignItem("a", "audio/a.flac", root / "a.flac", 1, "a" * 64, "", "", "campaign-a", manifest_index=1)
            append_campaign_ledger(ledger, {
                "status": "success", "id": "a", "source_sha256": "a" * 64, "source_bytes": 1,
                "contract": "same", "manifest_index": 1, "attempt_id": "attempt-a",
            })

            with self.assertRaises(CampaignLedgerError):
                pending_campaign_items([item], ledger, "same")
            with self.assertRaises(CampaignLedgerError):
                read_successful_item_ids(ledger, "same")

    def test_ledger_allows_only_a_malformed_final_line(self) -> None:
        from music_flamingo_campaign import CampaignLedgerError, read_successful_item_ids

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            good = {
                "status": "success",
                "id": "song-a",
                "source_sha256": "a" * 64,
                "source_bytes": 1,
                "contract": "same",
                **reusable_success_fields("output-a", 1),
            }
            final_damage = root / "final_damage.jsonl"
            final_damage.write_text(json.dumps(good) + "\n{\"partial\"", encoding="utf-8")
            self.assertEqual(read_successful_item_ids(final_damage, "same"), {"song-a"})

            middle_damage = root / "middle_damage.jsonl"
            middle_damage.write_text("{\"partial\"\n" + json.dumps(good) + "\n", encoding="utf-8")
            with self.assertRaises(CampaignLedgerError):
                read_successful_item_ids(middle_damage, "same")


class CampaignCliTests(unittest.TestCase):
    def test_validate_cli_prints_manifest_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_root = root / "input"
            (audio_root / "audio").mkdir(parents=True)
            data = b"audio"
            (audio_root / "audio/song.flac").write_bytes(data)
            manifest = root / "manifest.jsonl"
            write_jsonl(manifest, [manifest_row("song", "audio/song.flac", data)])

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "music_flamingo_campaign.py"),
                    "validate",
                    "--manifest",
                    str(manifest),
                    "--audio-root",
                    str(audio_root),
                    "--expected-count",
                    "1",
                ],
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["item_count"], 1)
            self.assertEqual(summary["campaign_id"], "campaign-a")

    def test_pending_cli_audits_unhydrated_manifest_without_fetching_all_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_root = root / "input"
            (audio_root / "audio").mkdir(parents=True)
            data = b"intentionally-not-hydrated"
            manifest = root / "manifest.jsonl"
            write_jsonl(manifest, [manifest_row("song", "audio/song.flac", data)])

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "music_flamingo_campaign.py"),
                    "pending",
                    "--manifest",
                    str(manifest),
                    "--audio-root",
                    str(audio_root),
                    "--expected-count",
                    "1",
                    "--expected-campaign-id",
                    "campaign-a",
                    "--ledger",
                    str(root / "campaign_ledger.jsonl"),
                    "--runtime-image",
                    "registry.example/music@sha256:" + "a" * 64,
                    "--prompt",
                    "prompt",
                    "--execution-profile",
                    "nvidia-l40/full_precision/bfloat16",
                    "--include-pending-ids",
                ],
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["reusable_success_count"], 0)
            self.assertEqual(report["pending_count"], 1)
            self.assertEqual(report["pending_item_ids"], ["song"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

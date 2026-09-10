"""Delivery adapter tests: adapted rows must pass the plugin's validator.

The acceptance seam is the existing ``load_campaign_delivery_file``; the
fixtures are real T1 probe outputs (thinking model, temp 1.0).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path


from moss_local_music.delivery import (  # noqa: E402
    TrackIdentity,
    build_delivery_row,
    write_delivery_jsonl,
)
from moss_local_music.runner import run_single, split_answer  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
PROBE_RAW = (FIXTURES / "probe-raw-bct.md").read_text(encoding="utf-8")
PROBE_ANSWER = (FIXTURES / "probe-answer-bct.txt").read_text(encoding="utf-8").strip()

TRACK = TrackIdentity(
    kugou_mix_song_id="937334621",
    title="保持童真",
    artist="示例歌手",
    relative_audio_path="music_downloads/KugouMusicClient/保持童真_MMM.mp3",
    source_url="https://www.kugou.com/mixsong/937334621.html",
)


def _probe_sidecar() -> dict:
    answer, think = split_answer(PROBE_RAW)
    assert answer == PROBE_ANSWER, "probe fixtures must agree after think-stripping"
    return {
        "schema_version": 1,
        "run_id": "probe",
        "item_id": "保持童真_MMM",
        "audio_path": "/audio/保持童真_MMM.mp3",
        "model_path": "/models/moss-8bit",
        "model_id": "mlx-community/MOSS-Music-8B-Thinking-8bit",
        "prompt_version": "v5",
        "prompt": "p",
        "prompt_sha256": hashlib.sha256(b"p").hexdigest(),
        "generation_controls": {"temp": 1.0, "top_p": 0.8, "top_k": 50, "max_new_tokens": 1500},
        "generated_token_count": 988,
        "elapsed_seconds": 1362.1,
        "generation_rate_tok_s": 0.7,
        "think_present": bool(think),
        "thinking": think,
        "answer": answer,
        "answer_path": "/out/保持童真_MMM.answer.txt",
        "raw_path": "/out/保持童真_MMM.raw.md",
        "created_utc": "2026-09-10T00:00:00+00:00",
    }


class BuildDeliveryRowTest(unittest.TestCase):
    def test_row_shape_and_hash_binding(self):
        row = build_delivery_row(
            _probe_sidecar(),
            TRACK,
            campaign_id="kugou-weekly-20260910",
            manifest_index=7,
            audio_sha256="a" * 64,
            audio_bytes=123,
        )
        self.assertEqual(row["id"], "kugou-937334621")
        self.assertEqual(row["campaign_id"], "kugou-weekly-20260910")
        self.assertEqual(row["manifest_index"], 7)
        self.assertEqual(row["contract"], "local-moss-music-v1")
        self.assertEqual(row["canonical_source"], "local-moss-music")
        self.assertEqual(row["title"], "保持童真")  # chart metadata, not model text
        self.assertEqual(row["output_text"], PROBE_ANSWER)
        self.assertEqual(
            row["output_text_sha256"],
            hashlib.sha256(PROBE_ANSWER.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(row["attempt_id"], "kugou-weekly-20260910-local-0007")
        self.assertNotIn("<think>", row["output_text"])


class DeliveryFileValidationTest(unittest.TestCase):
    def test_written_file_passes_existing_validator(self):
        from music_kb.campaign_delivery import load_campaign_delivery_file
        from music_kb.errors import ValidationError  # noqa: F401

        rows = [
            build_delivery_row(
                _probe_sidecar(),
                TrackIdentity(
                    kugou_mix_song_id=f"93733462{index}",
                    title=f"示例歌曲{index}",
                    artist="示例歌手",
                    relative_audio_path=f"audio/song-{index}.mp3",
                    source_url=f"https://www.kugou.com/mixsong/93733462{index}.html",
                ),
                campaign_id="kugou-weekly-20260910",
                manifest_index=index,
                audio_sha256=hashlib.sha256(f"audio-{index}".encode()).hexdigest(),
                audio_bytes=1000 + index,
            )
            for index in range(2)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_delivery_jsonl(rows, Path(tmp) / "canonical_delivery.jsonl")
            data = path.read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
            self.assertTrue(data.endswith(b"\n"))
            entries = load_campaign_delivery_file(path, expected_count=2)
            self.assertEqual(len(entries), 2)
            self.assertEqual(
                [entry.delivery_id for entry in entries],
                ["kugou-937334620", "kugou-937334621"],
            )

    def test_tampered_output_text_fails_validator(self):
        from music_kb.campaign_delivery import load_campaign_delivery_file
        from music_kb.errors import ValidationError

        row = build_delivery_row(
            _probe_sidecar(),
            TRACK,
            campaign_id="c",
            manifest_index=0,
            audio_sha256="a" * 64,
            audio_bytes=1,
        )
        row["output_text"] = row["output_text"] + " tampered"
        with tempfile.TemporaryDirectory() as tmp:
            path = write_delivery_jsonl([row], Path(tmp) / "delivery.jsonl")
            with self.assertRaises(ValidationError):
                load_campaign_delivery_file(path, expected_count=1)


class StubbedEndToEndTest(unittest.TestCase):
    def test_stub_generation_flows_through_to_valid_delivery(self):
        from music_kb.campaign_delivery import load_campaign_delivery_file

        def fake_generate(audio_path, model_path, prompt, **kwargs):
            return PROBE_RAW, 988, 12.0

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "保持童真_MMM.mp3"
            audio.write_bytes(b"\x00" * 2048)
            sidecar = run_single(
                str(audio),
                Path(tmp) / "analysis",
                model_path="/m",
                run_id="kugou-weekly-20260910",
                generate_fn=fake_generate,
            )
            row = build_delivery_row(
                sidecar,
                TRACK,
                campaign_id="kugou-weekly-20260910",
                manifest_index=0,
                audio_sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
                audio_bytes=audio.stat().st_size,
            )
            path = write_delivery_jsonl([row], Path(tmp) / "canonical_delivery.jsonl")
            entries = load_campaign_delivery_file(path, expected_count=1)
            self.assertEqual(entries[0].title, "保持童真")


if __name__ == "__main__":
    unittest.main()

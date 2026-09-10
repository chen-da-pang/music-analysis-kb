"""Batch runner tests: queue intake, resume, failure isolation, delivery.

All tests stub the generator at the model-call seam; no model is loaded.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER_DIR = Path(__file__).resolve().parents[1]

from moss_local_music.batch import (  # noqa: E402
    load_batch_items,
    run_batch,
)


def write_queue(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    return path


QUEUE_ROWS = [
    {
        "identity_key": "kugou:101",
        "platform_track_key": "101",
        "title": "歌一",
        "artist": "歌手一",
        "play_link": "https://www.kugou.com/mixsong/101.html",
    },
    {
        "identity_key": "kugou:102",
        "platform_track_key": "102",
        "title": "歌二",
        "artist": "歌手二",
        "play_link": "https://www.kugou.com/mixsong/102.html",
    },
    {
        "identity_key": "kugou:103",
        "platform_track_key": "103",
        "title": "歌三",
        "artist": "歌手三",
        "play_link": None,
    },
]


class BatchTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audio_root = self.root / "audio"
        self.audio_root.mkdir()
        self.analysis_dir = self.root / "analysis"
        self.queue = write_queue(self.root / "queue.jsonl", QUEUE_ROWS)
        # audio present for 101 and 102; 103 has no file on disk
        self.audio_files = {}
        for mix_id in ("101", "102"):
            f = self.audio_root / f"song-{mix_id}.flac"
            f.write_bytes(f"audio-bytes-{mix_id}".encode())
            self.audio_files[mix_id] = f
        inventory = {
            "schema_version": 1,
            "songs": [
                {
                    "identity_key": f"kugou:{mix_id}",
                    "platform_track_key": mix_id,
                    "download": {"status": "downloaded", "path": f"song-{mix_id}.flac"},
                }
                for mix_id in ("101", "102", "103")
            ],
        }
        self.inventory = self.root / "inventory.json"
        self.inventory.write_text(json.dumps(inventory), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def counting_generate(self, raw="<think>t</think>\n\n### 速度:\n88 bpm.\n"):
        calls = []

        def generate(audio_path, model_path, prompt, **kwargs):
            calls.append(audio_path)
            return raw, 10, 1.0

        generate.calls = calls
        return generate


class LoadBatchItemsTest(BatchTestBase):
    def test_joins_queue_inventory_and_audio_existence(self):
        items, excluded = load_batch_items(self.queue, self.inventory, self.audio_root)
        self.assertEqual([i.item_id for i in items], ["101", "102"])
        self.assertEqual(excluded, [("kugou:103", "audio_file_missing")])
        self.assertEqual(items[0].title, "歌一")
        self.assertEqual(items[0].audio_path, self.audio_files["101"])
        self.assertEqual(items[0].relative_audio_path, "song-101.flac")
        self.assertEqual(items[1].source_url, "https://www.kugou.com/mixsong/102.html")


class RunBatchTest(BatchTestBase):
    def test_happy_path_delivers_and_records_events(self):
        from music_kb.campaign_delivery import load_campaign_delivery_file

        gen = self.counting_generate()
        delivery = self.root / "delivery.jsonl"
        summary = run_batch(
            self.queue,
            inventory_path=self.inventory,
            audio_root=self.audio_root,
            analysis_dir=self.analysis_dir,
            delivery_path=delivery,
            run_id="run-batch",
            generate_fn=gen,
        )
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["skipped_no_audio"], 1)
        entries = load_campaign_delivery_file(delivery, expected_count=2)
        self.assertEqual(
            [e.delivery_id for e in entries], ["kugou-101", "kugou-102"]
        )
        self.assertEqual([e.manifest_index for e in entries], [0, 1])
        events = [
            json.loads(line)
            for line in (self.analysis_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [e["event"] for e in events],
            ["skipped_no_audio", "started", "completed", "started", "completed"],
        )
        self.assertEqual(events[0]["item_id"], "kugou:103")
        self.assertEqual(events[0]["reason"], "audio_file_missing")

    def test_resume_skips_completed_without_model_calls(self):
        from music_kb.campaign_delivery import load_campaign_delivery_file

        gen = self.counting_generate()
        delivery = self.root / "delivery.jsonl"
        run_batch(
            self.queue,
            inventory_path=self.inventory,
            audio_root=self.audio_root,
            analysis_dir=self.analysis_dir,
            delivery_path=delivery,
            run_id="run-batch",
            generate_fn=gen,
        )
        second = self.counting_generate()
        summary = run_batch(
            self.queue,
            inventory_path=self.inventory,
            audio_root=self.audio_root,
            analysis_dir=self.analysis_dir,
            delivery_path=delivery,
            run_id="run-batch",
            generate_fn=second,
        )
        self.assertEqual(second.calls, [])
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["resumed"], 2)
        entries = load_campaign_delivery_file(delivery, expected_count=2)
        self.assertEqual(len(entries), 2)

    def test_failed_item_is_isolated_and_batch_continues(self):
        from music_kb.campaign_delivery import load_campaign_delivery_file

        gen = self.counting_generate()

        def flaky(audio_path, model_path, prompt, **kwargs):
            if "song-101" in audio_path:
                raise RuntimeError("model exploded")
            return "<think>t</think>\n\nanswer 102.", 5, 0.5

        delivery = self.root / "delivery.jsonl"
        summary = run_batch(
            self.queue,
            inventory_path=self.inventory,
            audio_root=self.audio_root,
            analysis_dir=self.analysis_dir,
            delivery_path=delivery,
            run_id="run-batch",
            generate_fn=flaky,
        )
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["failed"], 1)
        entries = load_campaign_delivery_file(delivery, expected_count=1)
        self.assertEqual(entries[0].delivery_id, "kugou-102")
        events = [
            json.loads(line)
            for line in (self.analysis_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        failed_events = [e for e in events if e["event"] == "failed"]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0]["item_id"], "101")


if __name__ == "__main__":
    unittest.main()

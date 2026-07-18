#!/usr/bin/env python3
"""Tests for the resumable KuGou Music Flamingo campaign."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


class KugouCampaignInputTests(unittest.TestCase):
    def test_prepare_campaign_materializes_only_manifest_records(self) -> None:
        from prepare_kugou_campaign_input import materialize_campaign

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.flac"
            source.write_bytes(b"audio-bytes")
            progress = root / "download_progress.json"
            progress.write_text(
                json.dumps(
                    {
                        "total": 1,
                        "downloaded": {
                            "song-a": {
                                "file": str(source),
                                "title": "Song A",
                                "artist": "Artist B",
                                "ext": "flac",
                                "play_link": "https://www.kugou.com/mixsong/agent_gateway/song-a.html",
                            }
                        },
                        "failed": {},
                        "no_results": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            destination = root / "campaign"

            result = materialize_campaign(progress, destination, "campaign-a")

            rows = [
                json.loads(line)
                for line in (destination / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(result["campaign_id"], "campaign-a")
            self.assertEqual(result["item_count"], 1)
            self.assertEqual(rows, [{
                "id": "song-a",
                "relative_audio_path": "audio/song-a.flac",
                "source_bytes": len(b"audio-bytes"),
                "sha256": hashlib.sha256(b"audio-bytes").hexdigest(),
                "title": "Song A",
                "artist": "Artist B",
                "campaign_id": "campaign-a",
                "source_url": "https://www.kugou.com/mixsong/agent_gateway/song-a.html",
            }])
            staged = destination / rows[0]["relative_audio_path"]
            self.assertEqual(staged.read_bytes(), b"audio-bytes")
            self.assertTrue(staged.is_relative_to(destination))

    def test_prepare_campaign_accepts_extensionless_source_when_manifest_declares_type(self) -> None:
        from prepare_kugou_campaign_input import materialize_campaign

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "extensionless"
            source.write_bytes(b"fLaC" + b"audio-bytes")
            progress = root / "download_progress.json"
            progress.write_text(
                json.dumps(
                    {
                        "total": 1,
                        "downloaded": {
                            "song-a": {
                                "file": str(source),
                                "title": "Song A",
                                "artist": "Artist B",
                                "ext": "flac",
                            }
                        },
                        "failed": {},
                        "no_results": {},
                    }
                ),
                encoding="utf-8",
            )

            materialize_campaign(progress, root / "campaign", "campaign-a")

            self.assertTrue((root / "campaign/audio/song-a.flac").is_file())

    def test_prepare_campaign_rejects_unsafe_source_url(self) -> None:
        from prepare_kugou_campaign_input import CampaignInputError, materialize_campaign

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp3"
            source.write_bytes(b"audio-bytes")
            progress = root / "download_progress.json"
            progress.write_text(
                json.dumps(
                    {
                        "total": 2,
                        "downloaded": {
                            "song-a": {
                                "file": str(source),
                                "title": "Song A",
                                "artist": "Artist B",
                                "ext": "mp3",
                                "play_link": "file:///tmp/song-a.mp3",
                            }
                        },
                        "failed": {"song-b": {"reason": "not found"}},
                        "no_results": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CampaignInputError, "Unsafe source_url"):
                materialize_campaign(progress, root / "campaign", "campaign-a")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "prepare_weekly_cnb_campaign.py"


def _module():
    spec = importlib.util.spec_from_file_location("prepare_weekly_cnb_campaign", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_materialize_campaign_hardlinks_audio_and_writes_hash_manifest(tmp_path: Path) -> None:
    module = _module()
    audio_root = tmp_path / "audio-root"
    audio_root.mkdir()
    source = audio_root / "song.flac"
    source.write_bytes(b"audio-bytes")
    queue = tmp_path / "queue.jsonl"
    queue.write_text(json.dumps({"identity_key": "kugou:123"}) + "\n", encoding="utf-8")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "songs": [
                    {
                        "identity_key": "kugou:123",
                        "platform_track_key": "123",
                        "title": "Song",
                        "artist": "Artist",
                        "play_link": "https://www.kugou.com/song/123",
                        "download": {"status": "downloaded", "path": "song.flac"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    summary = module.materialize(queue, inventory, audio_root, tmp_path / "staging", "weekly-1")
    manifest = json.loads((tmp_path / "staging" / "manifest.jsonl").read_text(encoding="utf-8"))
    assert summary["item_count"] == 1
    assert summary["hardlinked"] == 1
    assert manifest["id"] == "kugou-123"
    assert manifest["source_bytes"] == len(b"audio-bytes")
    assert manifest["source_url"].startswith("https://")

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


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


def test_partial_materialization_requires_exact_downloaded_and_terminal_status_counts(
    tmp_path: Path,
) -> None:
    module = _module()
    audio_root = tmp_path / "audio-root"
    audio_root.mkdir()
    (audio_root / "one.flac").write_bytes(b"one")
    (audio_root / "two.flac").write_bytes(b"two")
    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        "\n".join(
            json.dumps({"identity_key": identity})
            for identity in ("kugou:1", "kugou:2", "kugou:3")
        )
        + "\n",
        encoding="utf-8",
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "songs": [
                    {
                        "identity_key": "kugou:1",
                        "platform_track_key": "1",
                        "title": "One",
                        "artist": "Artist",
                        "play_link": "https://www.kugou.com/song/1",
                        "download": {"status": "downloaded", "path": "one.flac"},
                    },
                    {
                        "identity_key": "kugou:2",
                        "platform_track_key": "2",
                        "title": "Two",
                        "artist": "Artist",
                        "play_link": "https://www.kugou.com/song/2",
                        "download": {"status": "downloaded", "path": "two.flac"},
                    },
                    {
                        "identity_key": "kugou:3",
                        "platform_track_key": "3",
                        "title": "Three",
                        "artist": "Artist",
                        "play_link": "https://www.kugou.com/song/3",
                        "download": {"status": "abandoned", "path": None},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    staging = tmp_path / "staging"
    summary = module.materialize(
        queue,
        inventory,
        audio_root,
        staging,
        "weekly-1",
        expected_source_count=3,
        expected_selected_count=2,
        expected_excluded_status_counts={"abandoned": 1},
    )
    receipt = json.loads((staging / "selection-receipt.json").read_text(encoding="utf-8"))
    assert summary["item_count"] == 2
    assert summary["source_queue_item_count"] == 3
    assert summary["excluded_by_download_status"] == {"abandoned": 1}
    assert receipt["selection_mode"] == "downloaded_with_explicit_terminal_exclusions"
    assert receipt["selected_item_count"] == 2
    assert receipt["excluded_identities"] == [
        {"identity_key": "kugou:3", "download_status": "abandoned"}
    ]


def test_partial_materialization_rejects_unapproved_or_mismatched_statuses(tmp_path: Path) -> None:
    module = _module()
    audio_root = tmp_path / "audio-root"
    audio_root.mkdir()
    (audio_root / "one.flac").write_bytes(b"one")
    queue = tmp_path / "queue.jsonl"
    queue.write_text(
        json.dumps({"identity_key": "kugou:1"})
        + "\n"
        + json.dumps({"identity_key": "kugou:2"})
        + "\n",
        encoding="utf-8",
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "songs": [
                    {
                        "identity_key": "kugou:1",
                        "platform_track_key": "1",
                        "title": "One",
                        "artist": "Artist",
                        "play_link": "https://www.kugou.com/song/1",
                        "download": {"status": "downloaded", "path": "one.flac"},
                    },
                    {
                        "identity_key": "kugou:2",
                        "download": {"status": "no_results", "path": None},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unapproved download status"):
        module.materialize(
            queue,
            inventory,
            audio_root,
            tmp_path / "staging",
            "weekly-1",
            expected_source_count=2,
            expected_selected_count=1,
            expected_excluded_status_counts={"abandoned": 1},
        )

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "prepare_download_queue.py"


def test_abandoned_records_stay_out_of_the_primary_queue_until_explicitly_requested(
    tmp_path: Path,
) -> None:
    source = tmp_path / "songs.json"
    source.write_text(
        json.dumps(
            [
                {
                    "mix_song_id": "123",
                    "song_name": "Terminal Song",
                    "artist_name": "Artist",
                }
            ]
        ),
        encoding="utf-8",
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "songs": [
                    {
                        "identity_key": "kugou:123",
                        "title_artist_key": "kugou:terminal song\\u0000artist",
                        "download": {
                            "status": "abandoned",
                            "fallback_attempts": 2,
                            "terminal_reason": "fallback_retry_limit_exhausted",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    audio_root = tmp_path / "audio"
    queue = tmp_path / "queue.jsonl"
    base = [
        sys.executable,
        str(SCRIPT),
        "--source",
        str(source),
        "--inventory",
        str(inventory),
        "--output",
        str(queue),
        "--audio-root",
        str(audio_root),
    ]

    skipped = subprocess.run(base, capture_output=True, text=True, check=True)
    skipped_manifest = json.loads(skipped.stdout)
    assert skipped_manifest["queued"] == 0
    assert skipped_manifest["skipped_abandoned"] == 1
    assert skipped_manifest["retry_abandoned"] is False
    assert queue.read_text(encoding="utf-8") == ""

    recovered = subprocess.run([*base, "--retry-abandoned"], capture_output=True, text=True, check=True)
    recovered_manifest = json.loads(recovered.stdout)
    assert recovered_manifest["queued"] == 1
    assert recovered_manifest["skipped_abandoned"] == 0
    assert recovered_manifest["retry_abandoned"] is True
    assert json.loads(queue.read_text(encoding="utf-8"))["identity_key"] == "kugou:123"

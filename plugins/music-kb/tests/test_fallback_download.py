from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "download_music_fallback.py"
PREPARE = Path(__file__).parents[1] / "scripts" / "prepare_fallback_queue.py"
PROFILE = Path(__file__).parents[1] / "references" / "fallback-download-profile.json"


def _module():
    spec = importlib.util.spec_from_file_location("download_music_fallback", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fallback_artist_matching_accepts_only_declared_aliases() -> None:
    module = _module()
    assert module.artists_match("万豹、羊羊", "万豹&羊羊", [])
    assert module.artists_match("DINO", "디노", [["DINO", "디노"]])
    assert module.artists_match("RESCENE", "RESCENE (리센느)", [["RESCENE", "RESCENE (리센느)"]])
    assert not module.artists_match("DINO", "Other", [["DINO", "디노"]])


def test_fallback_dry_run_does_not_touch_inventory_or_audio(tmp_path: Path) -> None:
    queue = tmp_path / "fallback_queue.jsonl"
    queue.write_text(json.dumps({"identity_key": "kugou:1", "title": "Song", "artist": "Artist"}) + "\n", encoding="utf-8")
    inventory = tmp_path / "inventory.json"
    original = {"songs": [{"identity_key": "kugou:1", "download": {"status": "no_results"}}]}
    inventory.write_text(json.dumps(original), encoding="utf-8")
    progress = tmp_path / "progress.json"
    audio = tmp_path / "audio"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--queue", str(queue),
            "--inventory", str(inventory),
            "--work-dir", str(audio),
            "--progress", str(progress),
            "--run-id", "fallback-test",
            "--profile", str(PROFILE),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    summary = json.loads(result.stdout)
    assert summary["queue"] == 1
    assert summary["would_process"] == 1
    assert json.loads(inventory.read_text(encoding="utf-8")) == original
    assert not progress.exists()
    assert not audio.exists()


def test_prepare_fallback_queue_only_includes_unique_no_results(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "songs": [
                    {"identity_key": "kugou:1", "title": "Beyond the Dream", "artist": "DINO", "download": {"status": "no_results"}},
                    {"identity_key": "kugou:1", "title": "Beyond the Dream", "artist": "DINO", "download": {"status": "no_results"}},
                    {"identity_key": "kugou:2", "title": "Done", "artist": "Artist", "download": {"status": "downloaded"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    queue = tmp_path / "fallback.jsonl"
    result = subprocess.run(
        [sys.executable, str(PREPARE), "--inventory", str(inventory), "--output", str(queue), "--profile", str(PROFILE)],
        capture_output=True,
        text=True,
        check=True,
    )
    manifest = json.loads(result.stdout)
    row = json.loads(queue.read_text(encoding="utf-8"))
    assert manifest["queued"] == 1
    assert manifest["unique_identity_keys"] == 1
    assert row["artist_aliases"] == [["DINO", "디노"]]

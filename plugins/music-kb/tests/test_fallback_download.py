from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "download_music_fallback.py"
PREPARE = Path(__file__).parents[1] / "scripts" / "prepare_fallback_queue.py"
LAUNCHER = Path(__file__).parents[1] / "scripts" / "launch_music_fallback_worker.py"
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


def test_detached_launcher_writes_completion_without_touching_dry_run_inventory(tmp_path: Path) -> None:
    queue = tmp_path / "fallback_queue.jsonl"
    queue.write_text(json.dumps({"identity_key": "kugou:1", "title": "Song", "artist": "Artist"}) + "\n", encoding="utf-8")
    inventory = tmp_path / "inventory.json"
    original = {"songs": [{"identity_key": "kugou:1", "download": {"status": "no_results"}}]}
    inventory.write_text(json.dumps(original), encoding="utf-8")
    launch = tmp_path / "launch.json"
    completion = tmp_path / "completion.json"
    result = subprocess.run(
        [
            sys.executable,
            str(LAUNCHER),
            "--queue", str(queue),
            "--inventory", str(inventory),
            "--work-dir", str(tmp_path / "audio"),
            "--progress", str(tmp_path / "progress.json"),
            "--run-id", "detached-test",
            "--profile", str(PROFILE),
            "--launch-receipt", str(launch),
            "--completion-receipt", str(completion),
            "--worker-log", str(tmp_path / "worker.log"),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    launched = json.loads(result.stdout)
    assert launched["status"] == "launched"
    deadline = time.monotonic() + 10
    while not completion.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    finished = json.loads(completion.read_text(encoding="utf-8"))
    assert finished["status"] == "succeeded"
    assert finished["exit_code"] == 0
    assert json.loads(inventory.read_text(encoding="utf-8")) == original


def test_detached_launcher_refuses_duplicate_receipt(tmp_path: Path) -> None:
    receipt = tmp_path / "launch.json"
    receipt.write_text("{}\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(LAUNCHER),
            "--queue", str(tmp_path / "queue.jsonl"),
            "--inventory", str(tmp_path / "inventory.json"),
            "--work-dir", str(tmp_path / "audio"),
            "--progress", str(tmp_path / "progress.json"),
            "--run-id", "duplicate-test",
            "--profile", str(PROFILE),
            "--launch-receipt", str(receipt),
            "--completion-receipt", str(tmp_path / "completion.json"),
            "--worker-log", str(tmp_path / "worker.log"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "refusing a duplicate worker" in result.stderr


def test_prepare_fallback_queue_only_includes_unique_retryable_statuses(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "songs": [
                    {"identity_key": "kugou:1", "title": "Beyond the Dream", "artist": "DINO", "download": {"status": "no_results"}},
                    {"identity_key": "kugou:1", "title": "Beyond the Dream", "artist": "DINO", "download": {"status": "no_results"}},
                    {"identity_key": "kugou:3", "title": "Retry", "artist": "Artist", "download": {"status": "failed"}},
                    {"identity_key": "kugou:2", "title": "Done", "artist": "Artist", "download": {"status": "downloaded"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    queue = tmp_path / "fallback.jsonl"
    result = subprocess.run(
        [sys.executable, str(PREPARE), "--inventory", str(inventory), "--output", str(queue), "--profile", str(PROFILE), "--statuses", "no_results,failed"],
        capture_output=True,
        text=True,
        check=True,
    )
    manifest = json.loads(result.stdout)
    rows = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines()]
    assert manifest["queued"] == 2
    assert manifest["unique_identity_keys"] == 2
    assert manifest["retry_statuses"] == ["no_results", "failed"]
    assert manifest["status_counts"] == {"no_results": 1, "failed": 1}
    assert rows[0]["artist_aliases"] == [["DINO", "디노"]]
    assert rows[0]["retry_from_status"] == "no_results"
    assert rows[1]["retry_from_status"] == "failed"


def test_prepare_fallback_queue_rejects_nonretryable_status(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    inventory.write_text('{"songs": []}\n', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PREPARE), "--inventory", str(inventory), "--output", str(tmp_path / "queue.jsonl"), "--profile", str(PROFILE), "--statuses", "no_results,downloaded"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "unsupported retry statuses: downloaded" in result.stderr

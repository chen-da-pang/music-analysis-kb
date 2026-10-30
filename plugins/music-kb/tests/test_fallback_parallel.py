from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "download_music_fallback.py"
PROFILE = Path(__file__).parents[1] / "references" / "fallback-download-profile.json"


def _module():
    spec = importlib.util.spec_from_file_location("download_music_fallback", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _profile(**overrides):
    profile = {
        "sources": ["MiguMusicClient", "QQMusicClient", "KuwoMusicClient"],
        "retry_statuses": ["no_results", "failed"],
        "max_fallback_attempts": 2,
        "source_timeout_seconds": 100,
        "minimum_size_bytes": 1_000_000,
        "minimum_duration_seconds": 60,
        "artist_aliases": {},
    }
    profile.update(overrides)
    return profile


def test_parallel_settings_requires_explicit_opt_in() -> None:
    module = _module()
    settings = module.parallel_settings(_profile())
    assert settings["source_parallel"] is False
    assert settings["outer_concurrency"] == module.DEFAULT_OUTER_CONCURRENCY
    assert settings["parallel_sources"] == module.DEFAULT_PARALLEL_SOURCES


def test_parallel_settings_reads_declared_fields() -> None:
    module = _module()
    settings = module.parallel_settings(
        _profile(
            source_parallel=True,
            parallel_sources=["NeteaseDirect", "MiguMusicClient"],
            per_song_deadline_seconds=90,
            outer_concurrency=8,
            search_timeout_seconds=30,
            download_timeout_seconds=120,
            lossless_transcode_128k=True,
        )
    )
    assert settings["source_parallel"] is True
    assert settings["parallel_sources"] == ["NeteaseDirect", "MiguMusicClient"]
    assert settings["per_song_deadline_seconds"] == 90
    assert settings["outer_concurrency"] == 8
    assert settings["search_timeout_seconds"] == 30
    assert settings["download_timeout_seconds"] == 120
    assert settings["lossless_transcode_128k"] is True


def test_parallel_settings_reject_unknown_source() -> None:
    module = _module()
    try:
        module.parallel_settings(_profile(source_parallel=True, parallel_sources=["NeteaseMusicClient"]))
    except ValueError as exc:
        assert "unsupported sources" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parallel_settings_reject_out_of_range_concurrency() -> None:
    module = _module()
    for value in (0, "8", 33):
        try:
            module.parallel_settings(_profile(source_parallel=True, outer_concurrency=value))
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {value!r}")


def test_first_completed_source_wins() -> None:
    module = _module()

    def attempt(source: str) -> dict:
        if source == "slow":
            time.sleep(5)
            return {"source": source, "attempt_status": "downloaded", "elapsed_ms": 5000}
        time.sleep(0.05)
        return {"source": source, "attempt_status": "downloaded", "elapsed_ms": 50}

    started = time.monotonic()
    winner, attempts, deadline_hit = module.run_parallel_attempts(["slow", "fast"], attempt, 2.0)
    elapsed = time.monotonic() - started
    assert winner is not None and winner["source"] == "fast"
    assert attempts == [winner]
    assert deadline_hit is False
    assert elapsed < 1.0


def test_deadline_produces_failed_outcome() -> None:
    module = _module()

    def attempt(source: str) -> dict:
        time.sleep(5)
        return {"source": source, "attempt_status": "downloaded"}

    winner, attempts, deadline_hit = module.run_parallel_attempts(["a", "b"], attempt, 0.2)
    assert winner is None
    assert deadline_hit is True
    outcome = module.combined_outcome(attempts, deadline_hit)
    assert outcome["attempt_status"] == "failed"
    assert "deadline" in outcome["error"]


def test_combined_outcome_distinguishes_no_match_from_errors() -> None:
    module = _module()
    no_match = module.combined_outcome(
        [{"source": "MiguMusicClient", "attempt_status": "no_results", "error": "no match"}], False
    )
    assert no_match["attempt_status"] == "no_results"
    failed = module.combined_outcome(
        [
            {"source": "MiguMusicClient", "attempt_status": "no_results", "error": "no match"},
            {"source": "QQMusicClient", "attempt_status": "failed", "error": "timeout"},
        ],
        False,
    )
    assert failed["attempt_status"] == "failed"
    assert "timeout" in failed["error"]


def test_record_attempt_outcome_writes_inventory_and_terminal_state() -> None:
    module = _module()
    item = {}
    status, result = module.record_attempt_outcome(
        item,
        {
            "attempt_status": "downloaded",
            "source": "MiguMusicClient",
            "path": "/tmp/song.mp3",
            "matched_title": "Song",
            "matched_artist": "Artist",
            "verified": {"size_bytes": 3_000_000, "duration_seconds": 200.0},
        },
        None,
        "no_results",
        2,
    )
    assert status == "downloaded"
    assert item["download"]["source"] == "MiguMusicClient"
    assert item["download"]["fallback_attempts"] == 1
    assert result["fallback_attempts"] == 1

    status, result = module.record_attempt_outcome(
        item,
        {"attempt_status": "failed", "source": None, "error": "no free url"},
        item["download"],
        "no_results",
        2,
    )
    assert status == "abandoned"
    assert item["download"]["status"] == "abandoned"
    assert result["terminal_reason"] == "fallback_retry_limit_exhausted"


def test_legacy_mode_keeps_no_results_semantics(monkeypatch) -> None:
    module = _module()
    settings = module.parallel_settings(_profile(max_fallback_attempts=3))
    item = {"identity_key": "kugou:1"}
    candidate = {"identity_key": "kugou:1", "title": "Song", "artist": "Artist"}

    def fake_download_from_source(source, candidate, aliases, work_dir, profile, settings, music_client_cls):
        return {"attempt_status": "no_results", "source": source, "error": "no match"}

    monkeypatch.setattr(module, "download_from_source", fake_download_from_source)
    identity, status, result = module.process_candidate(
        candidate, item, _profile(max_fallback_attempts=3), settings, Path("/tmp"), 3, object
    )
    assert identity == "kugou:1"
    assert status == "no_results"
    assert result["attempt_status"] == "no_results"
    assert len(result["parallel_attempts"]) == 3


def test_finalize_download_transcodes_large_lossless(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    source = tmp_path / "song.flac"
    source.write_bytes(b"x" * 1024)

    monkeypatch.setattr(
        module, "verify_file", lambda path, minimum_size, minimum_duration: {"size_bytes": 30_000_000, "duration_seconds": 200.0}
    )

    def fake_transcode(path):
        target = path.with_name(path.stem + "-128k.mp3")
        target.write_bytes(b"y" * 1024)
        path.unlink(missing_ok=True)
        return target, {"transcoded_from_lossless": True, "source_size_bytes": 30_000_000, "source_ext": ".flac"}

    monkeypatch.setattr(module, "transcode_lossless_to_128k", fake_transcode)
    path, verified, metadata = module.finalize_download(
        source,
        _profile(),
        {"lossless_transcode_128k": True},
    )
    assert path.name == "song-128k.mp3"
    assert metadata["transcoded_from_lossless"] is True
    assert verified["size_bytes"] == 1024


def test_dry_run_reports_parallel_profile(tmp_path: Path) -> None:
    queue = tmp_path / "queue.jsonl"
    queue.write_text(json.dumps({"identity_key": "kugou:1", "title": "Song", "artist": "Artist"}) + chr(10), encoding="utf-8")
    inventory = tmp_path / "inventory.json"
    original = {"songs": [{"identity_key": "kugou:1", "download": {"status": "no_results"}}]}
    inventory.write_text(json.dumps(original), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--queue", str(queue),
            "--inventory", str(inventory),
            "--work-dir", str(tmp_path / "audio"),
            "--progress", str(tmp_path / "progress.json"),
            "--run-id", "parallel-dry-run",
            "--profile", str(PROFILE),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    summary = json.loads(result.stdout)
    assert summary["source_parallel"] is True
    assert summary["outer_concurrency"] == 8
    assert summary["per_song_deadline_seconds"] == 120
    assert summary["would_process"] == 1
    assert json.loads(inventory.read_text(encoding="utf-8")) == original
    assert not (tmp_path / "progress.json").exists()


def test_dry_run_without_flag_keeps_legacy_config(tmp_path: Path) -> None:
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    profile.pop("source_parallel", None)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    queue = tmp_path / "queue.jsonl"
    queue.write_text(json.dumps({"identity_key": "kugou:1", "title": "Song", "artist": "Artist"}) + chr(10), encoding="utf-8")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"songs": []}), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--queue", str(queue),
            "--inventory", str(inventory),
            "--work-dir", str(tmp_path / "audio"),
            "--progress", str(tmp_path / "progress.json"),
            "--run-id", "legacy-dry-run",
            "--profile", str(profile_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    summary = json.loads(result.stdout)
    assert summary["source_parallel"] is False
    assert summary["outer_concurrency"] == 1
    assert summary["parallel_sources"] == ["MiguMusicClient", "QQMusicClient", "KuwoMusicClient"]

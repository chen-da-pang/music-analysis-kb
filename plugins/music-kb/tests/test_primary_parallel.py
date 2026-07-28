from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"
CONTROLLER = SCRIPTS / "primary_parallel.py"


def _module():
    spec = importlib.util.spec_from_file_location("primary_parallel_test", CONTROLLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(run_id: str, *, timeout_seconds: int = 60) -> SimpleNamespace:
    return SimpleNamespace(
        parallelism=2,
        worker_python=sys.executable,
        timeout_seconds=timeout_seconds,
        run_id=run_id,
        item_timeout_seconds=10.0,
        lookup_mode="exact-page-first",
        worker_delay=0.0,
    )


def _write_successful_worker(command: list[str]) -> None:
    queue = Path(command[command.index("--queue") + 1])
    inventory_path = Path(command[command.index("--inventory") + 1])
    work_dir = Path(command[command.index("--work-dir") + 1])
    progress_path = Path(command[command.index("--progress") + 1])
    lyric_path = Path(command[command.index("--lyrics-receipt") + 1])
    data = json.loads(inventory_path.read_text(encoding="utf-8"))
    results: dict[str, dict[str, str]] = {}
    lyric_lines: list[str] = []
    for song in data["songs"]:
        identity = song["identity_key"]
        media = work_dir / "KugouMusicClient" / f"song-{identity.split(':')[1]}" / "audio.mp3"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"verified-audio")
        relative = media.relative_to(work_dir / "KugouMusicClient")
        song["download"] = {
            "status": "downloaded",
            "retention": "retained",
            "path": relative.as_posix(),
            "size_bytes": media.stat().st_size,
        }
        results[identity] = {"status": "downloaded", "path": relative.as_posix()}
        lyric_lines.append(json.dumps({"identity_key": identity, "status": "pending"}) + "\n")
    inventory_path.write_text(json.dumps(data), encoding="utf-8")
    progress_path.write_text(
        json.dumps(
            {
                "finished_at": "2026-07-28T00:00:00Z",
                "results": results,
                "downloaded": {},
                "lyrics": {},
                "summary": {"downloaded": len(results), "skipped_existing": 0, "failed": 0, "no_results": 0},
            }
        ),
        encoding="utf-8",
    )
    lyric_path.write_text("".join(lyric_lines), encoding="utf-8")


def test_primary_shards_have_private_state_and_merge_once(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    workspace = tmp_path / "workspace"
    run_dir = workspace / "data" / "download_runs" / "primary-fixture"
    run_dir.mkdir(parents=True)
    queue = run_dir / "queue.jsonl"
    rows = [
        {"identity_key": "kugou:1", "title": "One", "artist": "A"},
        {"identity_key": "kugou:2", "title": "Two", "artist": "A"},
    ]
    queue.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    inventory = workspace / "data" / "song_inventory.json"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(
        json.dumps({"schema_version": 1, "songs": [dict(row, download={"status": "not_attempted"}) for row in rows]}),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            values = [str(value) for value in command]
            calls.append(values)
            _write_successful_worker(values)
            self.returncode = 0

        def communicate(self, timeout=None):
            assert timeout is not None
            return "{}", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(module.subprocess, "Popen", FakeProcess)
    status, state = module.execute_isolated_parallel(
        args=_args("primary-fixture"),
        scripts=SCRIPTS,
        workspace=workspace,
        run_dir=run_dir,
        queue=queue,
        inventory=inventory,
        work_dir=workspace / "music_downloads",
        progress=run_dir / "progress.json",
        lyrics_receipt=run_dir / "lyrics-receipts.jsonl",
        env={"https_proxy": "http://proxy"},
        started_at="2026-07-28T00:00:00Z",
    )

    assert status == 0
    assert len(calls) == 2
    assert len({call[call.index("--inventory") + 1] for call in calls}) == 2
    assert len({call[call.index("--progress") + 1] for call in calls}) == 2
    assert len({call[call.index("--lyrics-receipt") + 1] for call in calls}) == 2
    assert state["merge"]["media_directories_moved"] == 2
    merged = json.loads(inventory.read_text(encoding="utf-8"))
    for song in merged["songs"]:
        path = Path(song["download"]["path"])
        assert song["download"]["status"] == "downloaded"
        assert path.is_file()
        assert str(workspace / "music_downloads" / "KugouMusicClient") in str(path)
    assert len((run_dir / "lyrics-receipts.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_primary_incomplete_shard_never_merges_formal_state(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    workspace = tmp_path / "workspace"
    run_dir = workspace / "run"
    run_dir.mkdir(parents=True)
    queue = run_dir / "queue.jsonl"
    queue.write_text('{"identity_key":"kugou:1","title":"One","artist":"A"}\n', encoding="utf-8")
    inventory = workspace / "inventory.json"
    original = {"songs": [{"identity_key": "kugou:1", "download": {"status": "not_attempted"}}]}
    inventory.write_text(json.dumps(original), encoding="utf-8")

    class MissingProgress:
        def __init__(self, _command, **_kwargs):
            self.returncode = 0

        def communicate(self, timeout=None):
            return "", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(module.subprocess, "Popen", MissingProgress)
    status, state = module.execute_isolated_parallel(
        args=_args("incomplete"),
        scripts=SCRIPTS,
        workspace=workspace,
        run_dir=run_dir,
        queue=queue,
        inventory=inventory,
        work_dir=workspace / "audio",
        progress=run_dir / "progress.json",
        lyrics_receipt=run_dir / "lyrics.jsonl",
        env={},
        started_at="2026-07-28T00:00:00Z",
    )
    assert status == 2
    assert state["merge"] == "not_run"
    assert json.loads(inventory.read_text(encoding="utf-8")) == original
    assert not (run_dir / "progress.json").exists()


def test_primary_merge_refuses_inventory_drift(tmp_path: Path) -> None:
    module = _module()
    inventory = tmp_path / "inventory.json"
    inventory.write_text('{"songs": []}\n', encoding="utf-8")
    expected = module.sha256_file(inventory)
    inventory.write_text('{"songs": [{"identity_key": "changed"}]}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="inventory changed"):
        module.merge_isolated_shards(
            shards=[],
            inventory_path=inventory,
            work_dir=tmp_path / "audio",
            progress_path=tmp_path / "progress.json",
            lyrics_receipt_path=tmp_path / "lyrics.jsonl",
            run_id="drift",
            started_at="now",
            expected_inventory_sha256=expected,
            queue_path=tmp_path / "queue.jsonl",
        )

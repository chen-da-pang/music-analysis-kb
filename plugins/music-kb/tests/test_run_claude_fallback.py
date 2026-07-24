from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_claude_fallback.py"


def _module():
    spec = importlib.util.spec_from_file_location("run_claude_fallback", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fallback_uses_one_direct_worker_for_a_single_item_without_claude(monkeypatch, tmp_path: Path, capsys) -> None:
    module = _module()
    workspace = tmp_path / "workspace"
    inventory = workspace / "data" / "song_inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text('{"songs": []}\n', encoding="utf-8")
    operations = tmp_path / "operations.json"
    operations.write_text(
        json.dumps({"schema_version": 1, "operations": {"fallback_download": {"effective_method": "fixture"}}}),
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        values = [str(value) for value in command]
        calls.append(values)
        script = Path(values[1]).name
        if script == "prepare_fallback_queue.py":
            queue = Path(values[values.index("--output") + 1])
            queue.parent.mkdir(parents=True, exist_ok=True)
            queue.write_text('{"identity_key":"kugou:1"}\n', encoding="utf-8")
            return subprocess.CompletedProcess(values, 0, json.dumps({"queued": 1}), "")
        if script == "download_music_fallback.py":
            progress = Path(values[values.index("--progress") + 1])
            progress.write_text(
                json.dumps(
                    {
                        "finished_at": "2026-07-23T00:00:00Z",
                        "summary": {"downloaded": 1, "skipped_existing": 0, "failed": 0, "no_results": 0},
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(values, 0, json.dumps({"downloaded": 1}), "")
        raise AssertionError(values)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "run_claude_fallback.py",
            "--workspace",
            str(workspace),
            "--run-id",
            "fallback-fixture",
            "--operations-file",
            str(operations),
            "--profile",
            str(profile),
        ],
    )

    assert module.main() == 0

    summary = json.loads(capsys.readouterr().out)
    worker_calls = [call for call in calls if Path(call[1]).name == "download_music_fallback.py"]
    assert len(worker_calls) == 1
    assert all("claude" not in value.casefold() for call in calls for value in call)
    assert summary["executor"] == "direct"
    assert summary["requested_parallelism"] == 2
    assert summary["parallelism"] == 1
    assert summary["worker_exit_code"] == 0
    assert summary["claude_exit_code"] is None
    assert Path(summary["receipt"]).is_file()


def test_fallback_parallel_shards_merge_once_after_all_workers_finish(monkeypatch, tmp_path: Path, capsys) -> None:
    module = _module()
    workspace = tmp_path / "workspace"
    inventory = workspace / "data" / "song_inventory.json"
    inventory.parent.mkdir(parents=True)
    rows = [
        {"identity_key": "kugou:1", "title_artist_key": "one", "title": "One", "artist": "Artist", "download": {"status": "no_results"}},
        {"identity_key": "kugou:2", "title_artist_key": "two", "title": "Two", "artist": "Artist", "download": {"status": "no_results"}},
    ]
    inventory.write_text(json.dumps({"schema_version": 1, "songs": rows}), encoding="utf-8")
    operations = tmp_path / "operations.json"
    operations.write_text(json.dumps({"schema_version": 1, "operations": {"fallback_download": {"effective_method": "fixture"}}}), encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")

    def fake_run(command, **_kwargs):
        values = [str(value) for value in command]
        assert Path(values[1]).name == "prepare_fallback_queue.py"
        queue = Path(values[values.index("--output") + 1])
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text("".join(json.dumps({key: value for key, value in row.items() if key != "download"}) + "\n" for row in rows), encoding="utf-8")
        return subprocess.CompletedProcess(values, 0, json.dumps({"queued": 2}), "")

    popen_calls: list[list[str]] = []

    class FakeProcess:
        def __init__(self, command, **_kwargs):
            values = [str(value) for value in command]
            popen_calls.append(values)
            shard_queue = Path(values[values.index("--queue") + 1])
            shard_inventory = Path(values[values.index("--inventory") + 1])
            shard_work_dir = Path(values[values.index("--work-dir") + 1])
            shard_progress = Path(values[values.index("--progress") + 1])
            shard_rows = [json.loads(line) for line in shard_queue.read_text(encoding="utf-8").splitlines()]
            data = json.loads(shard_inventory.read_text(encoding="utf-8"))
            results = {}
            for song in data["songs"]:
                key = song["identity_key"]
                media = shard_work_dir / "QQMusicClient" / f"unit-{key.split(':')[1]}" / "track.mp3"
                media.parent.mkdir(parents=True, exist_ok=True)
                media.write_bytes(b"verified-media")
                song["download"] = {
                    "status": "downloaded",
                    "retention": "retained",
                    "path": str(media),
                    "source": "QQMusicClient",
                    "size_bytes": media.stat().st_size,
                    "duration_seconds": 120,
                }
                results[key] = {"status": "downloaded", "source": "QQMusicClient", "path": str(media)}
            shard_inventory.write_text(json.dumps(data), encoding="utf-8")
            shard_progress.write_text(json.dumps({"finished_at": "2026-07-23T00:00:00Z", "results": results, "summary": {"downloaded": len(shard_rows), "skipped_existing": 0, "failed": 0, "no_results": 0}}), encoding="utf-8")
            self.returncode = 0

        def communicate(self, timeout=None):
            assert timeout == 1800
            return "{\"downloaded\": 1}", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "run_claude_fallback.py",
            "--workspace",
            str(workspace),
            "--run-id",
            "parallel-fixture",
            "--operations-file",
            str(operations),
            "--profile",
            str(profile),
        ],
    )

    assert module.main() == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["execution"] == "direct_parallel"
    assert summary["parallelism"] == 2
    assert len(popen_calls) == 2
    assert len({call[call.index("--inventory") + 1] for call in popen_calls}) == 2
    merged = json.loads(inventory.read_text(encoding="utf-8"))
    for song in merged["songs"]:
        path = Path(song["download"]["path"])
        assert song["download"]["status"] == "downloaded"
        assert path.is_file()
        assert str(workspace / "music_downloads" / "KugouMusicClient") in str(path)
    assert summary["worker_progress"]["summary"]["downloaded"] == 2
    assert all(
        str(workspace / "music_downloads" / "KugouMusicClient") in result["path"]
        for result in summary["worker_progress"]["results"].values()
    )


def test_parallel_merge_refuses_to_overwrite_a_concurrently_changed_inventory(tmp_path: Path) -> None:
    module = _module()
    inventory = tmp_path / "inventory.json"
    inventory.write_text('{"songs": []}\n', encoding="utf-8")
    expected_sha = module.sha256_file(inventory)
    inventory.write_text('{"songs": [{"identity_key": "changed"}]}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="inventory changed"):
        module.merge_isolated_shards(
            shards=[],
            inventory_path=inventory,
            work_dir=tmp_path / "audio",
            progress_path=tmp_path / "progress.json",
            run_id="fixture",
            started_at="2026-07-23T00:00:00Z",
            expected_inventory_sha256=expected_sha,
        )
    assert not (tmp_path / "progress.json").exists()

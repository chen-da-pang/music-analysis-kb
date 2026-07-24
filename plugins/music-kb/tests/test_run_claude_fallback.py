from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"
WRAPPER = SCRIPTS / "run_claude_fallback.py"
CONTROLLER = SCRIPTS / "fallback_parallel.py"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operation_file(tmp_path: Path) -> Path:
    path = tmp_path / "operations.json"
    path.write_text(
        json.dumps({"schema_version": 1, "operations": {"fallback_download": {"effective_method": "fixture"}}}),
        encoding="utf-8",
    )
    return path


def _profile_file(tmp_path: Path) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"retry_statuses": ["no_results", "failed"]}), encoding="utf-8")
    return path


def test_direct_wrapper_starts_short_launcher_without_claude(monkeypatch, tmp_path: Path, capsys) -> None:
    module = _module(WRAPPER, "run_claude_fallback_direct")
    workspace = tmp_path / "workspace"
    inventory = workspace / "data" / "song_inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text('{"songs": []}\n', encoding="utf-8")
    operations = _operation_file(tmp_path)
    profile = _profile_file(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        values = [str(value) for value in command]
        calls.append(values)
        script = Path(values[1]).name
        if script == "prepare_fallback_queue.py":
            queue = Path(values[values.index("--output") + 1])
            queue.parent.mkdir(parents=True, exist_ok=True)
            queue.write_text('{"identity_key":"kugou:1"}\n', encoding="utf-8")
            return subprocess.CompletedProcess(
                values,
                0,
                json.dumps({"queued": 1, "retry_statuses": ["no_results", "failed"], "status_counts": {"no_results": 1, "failed": 0}}),
                "",
            )
        if script == "launch_music_fallback_worker.py":
            launch = Path(values[values.index("--launch-receipt") + 1])
            completion = Path(values[values.index("--completion-receipt") + 1])
            progress = Path(values[values.index("--progress") + 1])
            launch.write_text(json.dumps({"supervisor_pid": os.getpid(), "parallelism": 2}), encoding="utf-8")
            completion.write_text(json.dumps({"status": "succeeded", "exit_code": 0, "parallelism": 2}), encoding="utf-8")
            progress.write_text(
                json.dumps(
                    {
                        "finished_at": "2026-07-24T00:00:00Z",
                        "results": {"kugou:1": {"status": "downloaded"}},
                        "summary": {"downloaded": 1, "skipped_existing": 0, "failed": 0, "no_results": 0},
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(values, 0, json.dumps({"status": "launched"}), "")
        raise AssertionError(values)

    monkeypatch.setattr(module, "resolve_musicdl_python", lambda _explicit: sys.executable)
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
            "--proxy",
            "http://127.0.0.1:7890",
        ],
    )

    assert module.main() == 0

    summary = json.loads(capsys.readouterr().out)
    launcher_calls = [call for call in calls if Path(call[1]).name == "launch_music_fallback_worker.py"]
    assert len(launcher_calls) == 1
    launcher = launcher_calls[0]
    assert launcher[launcher.index("--parallelism") + 1] == "2"
    assert launcher[launcher.index("--proxy") + 1] == "http://127.0.0.1:7890"
    assert launcher[launcher.index("--workspace") + 1] == str(workspace.resolve())
    assert launcher[launcher.index("--run-dir") + 1].endswith("data/download_runs/fallback-fixture")
    assert all("claude" not in value.casefold() for call in calls for value in call)
    assert summary["executor"] == "direct"
    assert summary["execution"] == "direct_detached"
    assert summary["parallelism"] == 2
    assert summary["atom_exit_code"] == 0
    assert Path(summary["receipt"]).is_file()


def test_invalid_worker_python_stops_before_any_launcher(monkeypatch, tmp_path: Path) -> None:
    module = _module(WRAPPER, "run_claude_fallback_invalid_worker")
    workspace = tmp_path / "workspace"
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "song_inventory.json").write_text('{"songs": []}\n', encoding="utf-8")
    operations = _operation_file(tmp_path)
    profile = _profile_file(tmp_path)
    monkeypatch.setattr(module, "resolve_musicdl_python", lambda _explicit: (_ for _ in ()).throw(RuntimeError("no interpreter")))
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("launcher must not run"))
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "run_claude_fallback.py",
            "--workspace",
            str(workspace),
            "--run-id",
            "invalid-worker",
            "--operations-file",
            str(operations),
            "--profile",
            str(profile),
        ],
    )

    with pytest.raises(RuntimeError, match="no interpreter"):
        module.main()


def _write_successful_shard(shard_command: list[str]) -> None:
    queue = Path(shard_command[shard_command.index("--queue") + 1])
    inventory = Path(shard_command[shard_command.index("--inventory") + 1])
    work_dir = Path(shard_command[shard_command.index("--work-dir") + 1])
    progress = Path(shard_command[shard_command.index("--progress") + 1])
    rows = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines()]
    data = json.loads(inventory.read_text(encoding="utf-8"))
    results = {}
    for song in data["songs"]:
        identity = song["identity_key"]
        media = work_dir / "QQMusicClient" / f"unit-{identity.split(':')[1]}" / "track.mp3"
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
        results[identity] = {"status": "downloaded", "source": "QQMusicClient", "path": str(media)}
    inventory.write_text(json.dumps(data), encoding="utf-8")
    progress.write_text(
        json.dumps(
            {
                "finished_at": "2026-07-24T00:00:00Z",
                "results": results,
                "summary": {"downloaded": len(rows), "skipped_existing": 0, "failed": 0, "no_results": 0},
            }
        ),
        encoding="utf-8",
    )


def _parallel_args(run_id: str, *, timeout_seconds: int = 1800) -> SimpleNamespace:
    return SimpleNamespace(parallelism=2, worker_python=sys.executable, timeout_seconds=timeout_seconds, run_id=run_id)


def test_parallel_shards_use_private_state_and_merge_once(monkeypatch, tmp_path: Path) -> None:
    module = _module(CONTROLLER, "fallback_parallel_merge")
    workspace = tmp_path / "workspace"
    queue = workspace / "data" / "download_runs" / "parallel-fixture" / "fallback_queue.jsonl"
    queue.parent.mkdir(parents=True)
    rows = [
        {"identity_key": "kugou:1", "title_artist_key": "one", "title": "One", "artist": "Artist"},
        {"identity_key": "kugou:2", "title_artist_key": "two", "title": "Two", "artist": "Artist"},
    ]
    queue.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    inventory = workspace / "data" / "song_inventory.json"
    inventory.write_text(json.dumps({"schema_version": 1, "songs": [{**row, "download": {"status": "no_results"}} for row in rows]}), encoding="utf-8")
    calls: list[tuple[list[str], dict]] = []

    class FakeProcess:
        def __init__(self, command, **kwargs):
            values = [str(value) for value in command]
            calls.append((values, kwargs))
            _write_successful_shard(values)
            self.returncode = 0

        def communicate(self, timeout=None):
            assert timeout is not None and 0 < timeout <= 1800
            return "{\"downloaded\": 1}", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(module.subprocess, "Popen", FakeProcess)
    status, state = module.execute_isolated_parallel(
        args=_parallel_args("parallel-fixture"),
        scripts=SCRIPTS,
        workspace=workspace,
        run_dir=queue.parent,
        queue=queue,
        inventory=inventory,
        work_dir=workspace / "music_downloads" / "KugouMusicClient",
        progress=queue.parent / "fallback-progress.json",
        profile=tmp_path / "profile.json",
        env={"http_proxy": "http://127.0.0.1:7890", "https_proxy": "http://127.0.0.1:7890"},
        started_at="2026-07-24T00:00:00Z",
    )

    assert status == 0
    assert state["merge"]["media_directories_moved"] == 2
    assert len(calls) == 2
    assert len({call[0][call[0].index("--inventory") + 1] for call in calls}) == 2
    assert len({call[0][call[0].index("--progress") + 1] for call in calls}) == 2
    assert all(call[1]["env"]["http_proxy"] == "http://127.0.0.1:7890" for call in calls)
    merged = json.loads(inventory.read_text(encoding="utf-8"))
    for song in merged["songs"]:
        path = Path(song["download"]["path"])
        assert song["download"]["status"] == "downloaded"
        assert path.is_file()
        assert str(workspace / "music_downloads" / "KugouMusicClient") in str(path)


def test_parallel_merge_refuses_changed_inventory(tmp_path: Path) -> None:
    module = _module(CONTROLLER, "fallback_parallel_sha")
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
            started_at="2026-07-24T00:00:00Z",
            expected_inventory_sha256=expected_sha,
        )
    assert not (tmp_path / "progress.json").exists()


def test_incomplete_shard_never_merges_real_state(monkeypatch, tmp_path: Path) -> None:
    module = _module(CONTROLLER, "fallback_parallel_incomplete")
    workspace = tmp_path / "workspace"
    queue = workspace / "run" / "fallback_queue.jsonl"
    queue.parent.mkdir(parents=True)
    queue.write_text('{"identity_key":"kugou:1","title":"One","artist":"Artist"}\n', encoding="utf-8")
    inventory = workspace / "inventory.json"
    original = {"songs": [{"identity_key": "kugou:1", "download": {"status": "no_results"}}]}
    inventory.write_text(json.dumps(original), encoding="utf-8")

    class MissingProgressProcess:
        def __init__(self, _command, **_kwargs):
            self.returncode = 0

        def communicate(self, timeout=None):
            assert timeout is not None
            return "", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(module.subprocess, "Popen", MissingProgressProcess)
    status, state = module.execute_isolated_parallel(
        args=_parallel_args("incomplete"),
        scripts=SCRIPTS,
        workspace=workspace,
        run_dir=queue.parent,
        queue=queue,
        inventory=inventory,
        work_dir=workspace / "audio",
        progress=queue.parent / "fallback-progress.json",
        profile=tmp_path / "profile.json",
        env={},
        started_at="2026-07-24T00:00:00Z",
    )

    assert status == 2
    assert state["merge"] == "not_run"
    assert json.loads(inventory.read_text(encoding="utf-8")) == original
    assert not (queue.parent / "fallback-progress.json").exists()


def test_parallel_shards_share_one_total_timeout(monkeypatch, tmp_path: Path) -> None:
    module = _module(CONTROLLER, "fallback_parallel_timeout")
    workspace = tmp_path / "workspace"
    queue = workspace / "run" / "fallback_queue.jsonl"
    queue.parent.mkdir(parents=True)
    queue.write_text('{"identity_key":"kugou:1","title":"One","artist":"Artist"}\n', encoding="utf-8")
    inventory = workspace / "inventory.json"
    inventory.write_text('{"songs": [{"identity_key": "kugou:1", "download": {"status": "no_results"}}]}\n', encoding="utf-8")
    processes = []

    class TimedProcess:
        def __init__(self, _command, **_kwargs):
            self.returncode = 0
            self.timeouts = []
            self.killed = False
            processes.append(self)

        def communicate(self, timeout=None):
            self.timeouts.append(timeout)
            return "", ""

        def kill(self):
            self.killed = True
            self.returncode = -9

    monotonic_values = iter((0.0, 1.0, 10.1))
    monkeypatch.setattr(module.subprocess, "Popen", TimedProcess)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(monotonic_values))
    status, state = module.execute_isolated_parallel(
        args=_parallel_args("timeout", timeout_seconds=10),
        scripts=SCRIPTS,
        workspace=workspace,
        run_dir=queue.parent,
        queue=queue,
        inventory=inventory,
        work_dir=workspace / "audio",
        progress=queue.parent / "fallback-progress.json",
        profile=tmp_path / "profile.json",
        env={},
        started_at="2026-07-24T00:00:00Z",
    )

    assert status == 2
    assert state["merge"] == "not_run"
    assert processes[0].timeouts == [9.0]
    assert processes[1].killed
    assert processes[1].timeouts == [None]

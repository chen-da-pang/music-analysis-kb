from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_claude_download.py"


def _module():
    spec = importlib.util.spec_from_file_location("run_claude_download", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(identity: str) -> dict[str, str]:
    return {"identity_key": identity, "title": identity, "artist": "Artist"}


def test_pending_chunks_are_serial_and_skip_terminal_resume_rows() -> None:
    module = _module()
    rows = [_row(f"kugou:{index}") for index in range(1, 8)]
    progress = {
        "results": {
            "kugou:1": {"status": "downloaded"},
            "kugou:2": {"status": "no_results"},
            "kugou:3": {"status": "failed"},
        }
    }

    chunks = module.pending_chunks(rows, progress, chunk_size=2, max_items=None)

    assert [[module.row_identity(row) for row in chunk] for chunk in chunks] == [
        ["kugou:4", "kugou:5"],
        ["kugou:6", "kugou:7"],
    ]


def test_aggregate_progress_requires_every_original_queue_identity() -> None:
    module = _module()
    rows = [_row("kugou:1"), _row("kugou:2"), _row("kugou:3"), _row("kugou:4")]
    progress = {
        "results": {
            "kugou:1": {"status": "downloaded"},
            "kugou:2": {"status": "skipped_existing"},
            "kugou:3": {"status": "no_results"},
        }
    }

    result = module.aggregate_progress(rows, progress)

    assert result == {
        "queue": 4,
        "downloaded": 1,
        "skipped_existing": 1,
        "failed": 0,
        "no_results": 1,
        "unresolved": ["kugou:4"],
    }


def test_max_items_caps_pending_rows_without_changing_chunk_size() -> None:
    module = _module()
    rows = [_row(f"kugou:{index}") for index in range(1, 10)]

    chunks = module.pending_chunks(rows, {"results": {}}, chunk_size=3, max_items=5)

    assert [len(chunk) for chunk in chunks] == [3, 2]


def test_stale_downloaded_progress_is_retried_when_inventory_file_is_missing(tmp_path: Path) -> None:
    module = _module()
    row = _row("kugou:stale")
    progress = {"results": {"kugou:stale": {"status": "downloaded"}}}
    inventory = {"songs": [{"identity_key": "kugou:stale", "download": {"status": "missing", "path": "old.mp3"}}]}

    chunks = module.pending_chunks(
        [row], progress, chunk_size=25, max_items=None, inventory=inventory, audio_root=tmp_path
    )

    assert [[module.row_identity(item) for item in chunk] for chunk in chunks] == [["kugou:stale"]]


def test_pending_rows_caps_one_direct_execution_queue_without_requeueing_terminal_rows() -> None:
    module = _module()
    rows = [_row(f"kugou:{index}") for index in range(1, 6)]
    progress = {
        "results": {
            "kugou:1": {"status": "downloaded"},
            "kugou:2": {"status": "no_results"},
        }
    }

    pending = module.pending_rows(rows, progress, max_items=2)

    assert [module.row_identity(row) for row in pending] == ["kugou:3", "kugou:4"]


def test_direct_executor_runs_one_worker_without_claude(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    module = _module()
    workspace = tmp_path / "workspace"
    source = workspace / "source.json"
    source.parent.mkdir(parents=True)
    source.write_text("[]\n", encoding="utf-8")
    operations = tmp_path / "operations.json"
    operations.write_text(
        json.dumps({"schema_version": 1, "operations": {"claude_download": {"effective_method": "fixture"}}}),
        encoding="utf-8",
    )
    rows = [_row("kugou:1"), _row("kugou:2")]
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        values = [str(value) for value in command]
        calls.append(values)
        script = Path(values[1]).name
        if script == "build_song_inventory.py":
            inventory = Path(values[values.index("--inventory") + 1])
            inventory.parent.mkdir(parents=True, exist_ok=True)
            inventory.write_text(json.dumps({"songs": []}), encoding="utf-8")
            return subprocess.CompletedProcess(values, 0, json.dumps({"songs": 0}), "")
        if script == "prepare_download_queue.py":
            queue = Path(values[values.index("--output") + 1])
            queue.parent.mkdir(parents=True, exist_ok=True)
            queue.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            return subprocess.CompletedProcess(values, 0, json.dumps({"queued": 2}), "")
        if script == "download_music_queue.py":
            direct_queue = Path(values[values.index("--queue") + 1])
            assert direct_queue.name == "download-queue-direct.jsonl"
            assert values[values.index("--lookup-mode") + 1] == "exact-page-first"
            assert [json.loads(line)["identity_key"] for line in direct_queue.read_text(encoding="utf-8").splitlines()] == [
                "kugou:1",
                "kugou:2",
            ]
            inventory = Path(values[values.index("--inventory") + 1])
            work_dir = Path(values[values.index("--work-dir") + 1])
            audio_root = work_dir / "KugouMusicClient"
            audio_root.mkdir(parents=True, exist_ok=True)
            (audio_root / "one.mp3").write_bytes(b"one")
            inventory.write_text(
                json.dumps(
                    {
                        "songs": [
                            {"identity_key": "kugou:1", "download": {"status": "downloaded", "path": "one.mp3"}},
                            {"identity_key": "kugou:2", "download": {"status": "no_results", "path": None}},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            progress = Path(values[values.index("--progress") + 1])
            progress.write_text(
                json.dumps(
                    {
                        "run_id": "direct-fixture",
                        "finished_at": "2026-07-23T00:00:00Z",
                        "results": {
                            "kugou:1": {"status": "downloaded"},
                            "kugou:2": {"status": "no_results"},
                        },
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
            "run_claude_download.py",
            "--workspace",
            str(workspace),
            "--source",
            str(source),
            "--run-id",
            "direct-fixture",
            "--operations-file",
            str(operations),
        ],
    )

    assert module.main() == 0

    summary = json.loads(capsys.readouterr().out)
    worker_calls = [call for call in calls if Path(call[1]).name == "download_music_queue.py"]
    assert len(worker_calls) == 1
    assert all("claude" not in value.casefold() for call in calls for value in call)
    assert summary["executor"] == "direct"
    assert summary["lookup_mode"] == "exact-page-first"
    assert summary["queued_for_attempt"] == 2
    assert summary["worker_progress"] == {
        "queue": 2,
        "downloaded": 1,
        "skipped_existing": 0,
        "failed": 0,
        "no_results": 1,
        "unresolved": [],
    }
    assert summary["chunks"][0]["worker_exit_code"] == 0
    assert summary["timing"]["worker_ms"] >= 0


def test_claude_prompt_uses_one_monitor_wait_and_default_chunk_is_bounded() -> None:
    module = _module()
    prompt = module.render_prompt("python3 worker.py", chunk_index=1, chunk_total=3)

    assert module.DEFAULT_CHUNK_SIZE == 8
    assert "Monitor" in prompt
    assert "while/kill/sleep" in prompt

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_claude_fallback.py"


def _module():
    spec = importlib.util.spec_from_file_location("run_claude_fallback", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fallback_defaults_to_one_direct_worker_without_claude(monkeypatch, tmp_path: Path, capsys) -> None:
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
    assert summary["worker_exit_code"] == 0
    assert summary["claude_exit_code"] is None
    assert Path(summary["receipt"]).is_file()

from __future__ import annotations

import json
from pathlib import Path

from music_kb.preflight import run_preflight


def _operations(path: Path) -> None:
    path.write_text(json.dumps({"schema_version": 1, "operations": {"preflight": {"effective_method": "check"}}}), encoding="utf-8")


def test_preflight_reports_missing_command_and_peer_config(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    _operations(operations)
    database = tmp_path / "master.sqlite"
    inventory = tmp_path / "song_inventory.json"
    database.write_text("db", encoding="utf-8")
    inventory.write_text("{}", encoding="utf-8")

    result = run_preflight(
        workspace=tmp_path,
        operations_file=operations,
        database=database,
        inventory=inventory,
        audio_root=tmp_path / "audio",
        peers_file=tmp_path / "missing-peers.toml",
        publish=True,
        required_commands=("command-that-cannot-exist",),
    )

    assert result["valid"] is False
    assert {item["name"] for item in result["failed_required"]} == {
        "command:command-that-cannot-exist",
        "peers_file",
    }


def test_preflight_passes_without_peer_file_for_non_publishing_run(tmp_path: Path) -> None:
    operations = tmp_path / "operations.json"
    _operations(operations)
    (tmp_path / "master.sqlite").write_text("db", encoding="utf-8")
    (tmp_path / "song_inventory.json").write_text("{}", encoding="utf-8")

    result = run_preflight(
        workspace=tmp_path,
        operations_file=operations,
        database=tmp_path / "master.sqlite",
        inventory=tmp_path / "song_inventory.json",
        audio_root=tmp_path / "audio",
        peers_file=None,
        publish=False,
        required_commands=(),
    )

    assert result["valid"] is True

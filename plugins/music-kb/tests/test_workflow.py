from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from music_kb.publish_state import load_publish_state
from music_kb.schema import initialize_database
from music_kb.workflow import run_weekly_update


FIXTURE = Path(__file__).parent / "fixtures" / "analysis.json"
CAMPAIGN_FIXTURE = Path(__file__).parent / "fixtures" / "kugou_canonical_delivery.jsonl"


def _peers(path: Path, key: Path) -> None:
    path.write_text(
        f'''version = 2
[defaults]
identity_file = "{key}"
[[peers]]
name = "fixture-peer"
enabled = true
host = "fixture.example"
user = "music"
''',
        encoding="utf-8",
    )


class NoTransport:
    def __call__(self, command: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"transport must not run during prepare mode: {command} {timeout_seconds}")


class SuccessfulTransport:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, stdout="ok", stderr="")


def test_weekly_update_prepares_release_and_only_dry_runs_publish(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    peers = tmp_path / "peers.toml"
    _peers(peers, key)
    state = tmp_path / "publish-state.json"

    result = run_weekly_update(
        database=database,
        input_path=FIXTURE,
        input_kind="generic",
        expected_count=None,
        batch_size=1,
        output_dir=tmp_path / "releases",
        release_name="music-kb-atom3",
        peers_file=peers,
        publish=False,
        state_file=state,
        runner=NoTransport(),
    )

    assert result["workflow"] == "weekly-update"
    assert result["import"]["count"] == 1
    assert result["tags"]["skipped"] is True
    assert result["validation"]["valid"] is True
    assert result["release_verification"]["valid"] is True
    assert result["local_install"]["status"] == "skipped"
    assert result["publish"]["dry_run"] is True
    assert result["publish"]["peers"][0]["status"] == "planned"
    assert not state.exists()


def test_weekly_update_publish_records_state_after_verified_fanout(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    peers = tmp_path / "peers.toml"
    _peers(peers, key)
    state = tmp_path / "publish-state.json"
    runner = SuccessfulTransport()

    result = run_weekly_update(
        database=database,
        input_path=FIXTURE,
        input_kind="generic",
        expected_count=None,
        batch_size=1,
        output_dir=tmp_path / "releases",
        release_name="music-kb-atom3-publish",
        peers_file=peers,
        publish=True,
        state_file=state,
        runner=runner,
    )

    assert result["publish"]["dry_run"] is False
    assert result["publish"]["succeeded_count"] == 1
    assert result["local_install"]["status"] == "succeeded"
    assert (tmp_path / "current.sqlite").is_symlink()
    assert (tmp_path / "current.sqlite").resolve().name == "music-kb-atom3-publish.sqlite"
    assert len(runner.commands) == 6
    saved = load_publish_state(state)
    assert saved["last_publish"]["release_name"] == "music-kb-atom3-publish"
    assert saved["peers"]["fixture-peer"]["last_success"]["status"] == "succeeded"
    assert saved["peers"]["fixture-peer"]["last_success"]["release_sha256"] == result["release_verification"]["sha256"]


def test_campaign_weekly_update_rejects_missing_source_links(tmp_path: Path) -> None:
    database = tmp_path / "master.sqlite"
    initialize_database(database)
    key = tmp_path / "id_ed25519"
    key.write_text("fixture", encoding="utf-8")
    peers = tmp_path / "peers.toml"
    _peers(peers, key)

    with pytest.raises(ValueError, match="Source-link completeness gate failed"):
        run_weekly_update(
            database=database,
            input_path=CAMPAIGN_FIXTURE,
            input_kind="campaign",
            expected_count=2,
            batch_size=1,
            output_dir=tmp_path / "releases",
            release_name="campaign-link-gate",
            peers_file=peers,
            publish=False,
            state_file=tmp_path / "publish-state.json",
            runner=NoTransport(),
        )
